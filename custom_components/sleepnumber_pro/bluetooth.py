"""BLE transport for Sleep Number BAM hubs (MCR protocol).

The hub carries its own BLE radio, so this is the no-root local path: reach the
bed over Bluetooth (a local adapter or an ESPHome Bluetooth Proxy) and speak the
MCR binary protocol directly. Crucially, the foundation/base — which the cloud
now reports as "No Foundation Device" — is fully reachable here, so BLE recovers
base control the cloud severed.

Exposes the same ``available()`` / ``status()`` surface as the HTTP bridge so the
coordinator can prefer it identically, plus control methods for firmness and
foundation presets.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging

from . import mcr
from . import bamkey_ascii
from .const import SOURCE_BLE
from .local import LocalStatus

_LOGGER = logging.getLogger(__name__)

SERVICE_UUID = "ffffd1fd-388d-938b-344a-939d1f6efee0"
TX_UUID = "ffffd1fd-388d-938b-344a-939d1f6efee1"  # notify: bed -> us
RX_UUID = "ffffd1fd-388d-938b-344a-939d1f6efee2"  # write:  us -> bed

RESPONSE_TIMEOUT = 6.0


class BleBridgeClient:
    """Connect-on-demand MCR client over BLE."""

    source_name = SOURCE_BLE

    def __init__(self, hass, address: str) -> None:
        self.hass = hass
        self.address = address
        self.bed_addr = mcr.bed_addr_from_mac(address)
        self._lock = asyncio.Lock()
        # ASCII bamkey plane lifecycle (360-gen controller). Instance state —
        # baselines are per-connection learned values, never class-level.
        self.mode = "unknown"   # "ascii" | "mcr" | "unknown"
        self.baselines = {}     # side → rolling empty-bed pressure

    def _ble_device(self):
        """Resolve a connectable BLEDevice via HA's Bluetooth stack, or None."""
        from homeassistant.components import bluetooth

        return bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )

    async def available(self) -> bool:
        return self._ble_device() is not None

    async def _run(self, queries: list[tuple[bytes, int]]) -> dict[int, bytes]:
        """Connect, handshake, send each (frame, expected_func) query, and return
        {func: payload} for the responses seen. Serialised by a lock so we never
        hold two GATT connections (an ESP32 proxy allows only one)."""
        from bleak_retry_connector import establish_connection

        device = self._ble_device()
        if device is None:
            raise BleUnavailable(f"{self.address} not in range / no proxy")

        async with self._lock:
            buffer = bytearray()
            responses: dict[int, bytes] = {}
            waiting_for: set[int] = set()
            event = asyncio.Event()

            def _on_notify(_char, data: bytearray) -> None:
                buffer.extend(data)
                # Extract complete frames (payload length lives in header byte 9).
                while len(buffer) >= 14 and buffer[0:2] == mcr.SYNC:
                    payload_len = buffer[11] & 0x0F
                    total = 2 + 10 + payload_len + 2
                    if len(buffer) < total:
                        break
                    frame = bytes(buffer[:total])
                    del buffer[:total]
                    parsed = mcr.parse_frame(frame)
                    if parsed is not None:
                        responses[parsed.func] = parsed.payload
                        if parsed.func in waiting_for:
                            event.set()

            client = await establish_connection(
                __import__("bleak").BleakClient, device, self.address
            )
            try:
                await client.start_notify(TX_UUID, _on_notify)
                # Handshake first; the bed ignores queries without it.
                await client.write_gatt_char(RX_UUID, mcr.build_init(), response=True)
                await asyncio.sleep(0.2)
                for frame, expect_func in queries:
                    waiting_for = {expect_func}
                    event.clear()
                    # Proxy quirk: MCR RX must be written WITH response.
                    await client.write_gatt_char(RX_UUID, frame, response=True)
                    try:
                        await asyncio.wait_for(event.wait(), RESPONSE_TIMEOUT)
                    except TimeoutError:
                        _LOGGER.debug("No BLE response for func %s", expect_func)
                return responses
            finally:
                with contextlib.suppress(Exception):
                    await client.stop_notify(TX_UUID)
                await client.disconnect()

    async def status(self) -> LocalStatus:
        """Read sleep numbers over BLE. (Presence is broken in MCR firmware, so it
        stays None here and is filled from the cloud/pressure plane.)"""
        responses = await self._run(
            [(mcr.build_read_pump_for(self.bed_addr), mcr.FUNC_READ)]
        )
        pump = mcr.parse_pump_status(responses.get(mcr.FUNC_READ, b""))
        if pump is None:
            raise BleUnavailable("no pump status")
        return LocalStatus(
            sleep_number_left=pump.left_sleep_number,
            sleep_number_right=pump.right_sleep_number,
        )

    async def foundation_status(self):
        responses = await self._run(
            [(mcr.build_foundation_status(self.bed_addr), mcr.FUNC_READ)]
        )
        return mcr.parse_foundation_status(responses.get(mcr.FUNC_READ, b""))

    async def set_sleep_number(self, side: int, value: int) -> None:
        await self._run([(mcr.build_set_sleep_number(self.bed_addr, side, value), mcr.FUNC_SET)])

    async def activate_preset(self, side: int, preset: int) -> None:
        await self._run([(mcr.build_preset(self.bed_addr, side, preset), mcr.FUNC_PRESET)])

    # ------------------------------------------------------------------
    # ASCII bamkey plane (360-gen hub, wire-verified 2026-11-08): the hub's
    # UART characteristic accepts fUzIoN-framed ASCII commands and returns
    # PASS: responses read from the SAME char. See bamkey_ascii.py + the RE
    # workspace bamkey_reference.md. Feature methods below; the coordinator
    # prefers these when ASCII detection answers (BED supports all of them
    # regardless of the MCR firmware plane).
    # ------------------------------------------------------------------

    ASCII_BATCH = [  # live BAMG order (dashboard query)
        ("UBLG", ""), ("SNCG", "left"), ("SNFG", "left"),
        ("ACTG", "left head"), ("ACTG", "left foot"), ("ACGP", "left"), ("AGCP", "left"),
        ("UBAG", ""), ("SNCG", "right"), ("SNFG", "right"),
        ("ACTG", "right head"), ("ACTG", "right foot"), ("ACGP", "right"), ("AGCP", "right"),
    ]

    # ------------------------------------------------------------------
    # BlueZ pairing: the controller requires a bonded central before it
    # answers writes ([org.bluez.Error.NotPermitted] Not paired). HA uses
    # the host's BlueZ over dbus, so we create the bond ourselves and only
    # fall back to "put the bed in pairing mode" guidance when that fails.
    # ------------------------------------------------------------------

    @staticmethod
    def _is_not_paired_error(err: Exception) -> bool:
        """Match every observed refusal shape for writes without a bond:
        BlueZ-side refusals (Not paired / NotPermitted) and bed-side ATT
        errors on an unencrypted link (GATT error 15 = Insufficient
        encryption, 13 = Insufficient authentication, 14 = Unlikely Error
        seen right after the 15s on this controller)."""
        text = str(err).lower()
        return any(
            marker in text
            for marker in (
                "not paired",
                "notpermitted",
                "insufficient encryption",
                "insufficient authentication",
                "unlikely error",
            )
        )

    async def _async_pair_via_bluez(self) -> None:
        """Pair + trust the bed via the BlueZ Device1 API — one-time bond.

        Idempotent: org.bluez.Error.AlreadyExists (= already bonded) counts
        as done. Writes the bond on whatever BlueZ backs HA's adapter, so it
        behaves exactly like pairing from the SleepIQ app or bluetoothctl.
        """
        try:
            from dbus_fast import BusType, Message, MessageType, Variant
            from dbus_fast.aio.message_bus import MessageBus
        except ImportError as err:
            raise RuntimeError("dbus_fast (bleak dependency) unavailable — cannot self-pair") from err

        path = "/org/bluez/dev_" + self.address.replace(":", "_").upper()
        agent_registered = False
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        try:
            # BlueZ nests devices under their adapter ("hci0/dev_…") — never
            # guess the hci index; enumerate ObjectManager and find the
            # Device1 object for this address wherever it lives.
            listing = await bus.call(Message(
                destination="org.bluez", path="/",
                interface="org.freedesktop.DBus.ObjectManager",
                member="GetManagedObjects",
            ))
            want = "/dev_" + self.address.replace(":", "_").lower()
            path = None
            for obj_path, ifaces in (listing.body[0] if listing.body else {}).items():
                if "org.bluez.Device1" in ifaces and obj_path.lower().endswith(want):
                    path = obj_path
                    break
            if path is None:
                raise RuntimeError(
                    "BlueZ has no Device object for this address (device never "
                    "discovered by BlueZ) — retry once HA has seen its advertisement"
                )
            # Pairing requires a registered BlueZ agent. Server hosts run
            # bluetoothd with none, and Pair() then fails immediately with
            # AuthenticationFailed (or, mid pairing window, cancels at the
            # confirmation step). Register our own silent agent for the
            # attempt: NoInputNoOutput = Just Works, no user interaction.
            from dbus_fast.service import ServiceInterface, dbus_method

            class _JustWorksAgent(ServiceInterface):
                def __init__(self) -> None:
                    super().__init__("org.bluez.Agent1")

                @dbus_method()
                def Release(self) -> None:
                    pass

                # dbus-fast requires D-Bus signature STRINGS as method
                # annotations ("s"/"u"), not python types.
                @dbus_method()
                def RequestPinCode(self, device: "s") -> "s":
                    return "000000"

                @dbus_method()
                def RequestPasskey(self, device: "s") -> "u":
                    return 0

                @dbus_method()
                def DisplayPinCode(self, device: "s", pin_code: "s") -> None:
                    pass

                @dbus_method()
                def RequestConfirmation(self, device: "s", passkey: "u") -> None:
                    pass

                @dbus_method()
                def RequestAuthorization(self, device: "s") -> None:
                    pass

                @dbus_method()
                def AuthorizeService(self, device: "s", uuid: "s") -> None:
                    pass

                @dbus_method()
                def Cancel(self) -> None:
                    pass

            agent = _JustWorksAgent()
            agent_path = "/custom_components/sleepnumber_pro/pairing_agent"
            bus.export(agent_path, agent)
            await bus.call(Message(
                destination="org.bluez", path="/org/bluez",
                interface="org.bluez.AgentManager1", member="RegisterAgent",
                body=[agent_path, "NoInputNoOutput"], signature="ss",
            ))
            agent_registered = True
            async with asyncio.timeout(30):
                reply = await bus.call(Message(
                    destination="org.bluez", path=path,
                    interface="org.bluez.Device1", member="Pair",
                ))
            if reply.message_type == MessageType.ERROR:
                err_name = reply.error_name or ""
                if "AlreadyExists" not in err_name:
                    raise RuntimeError(f"BlueZ Pair() failed: {err_name}")
            await bus.call(Message(
                destination="org.bluez", path=path,
                interface="org.freedesktop.DBus.Properties", member="Set",
                body=["org.bluez.Device1", "Trusted", Variant("b", True)],
                signature="ssv",
            ))
            _LOGGER.info(
                "BlueZ bond with %s established and trusted — controller writes should now be accepted",
                self.address,
            )
        finally:
            if agent_registered:
                with contextlib.suppress(Exception):
                    await bus.call(Message(
                        destination="org.bluez", path="/org/bluez",
                        interface="org.bluez.AgentManager1",
                        member="UnregisterAgent",
                        body=["/custom_components/sleepnumber_pro/pairing_agent"],
                        signature="s",
                    ))
            bus.disconnect()

    async def _pair_and_retry(self, client, uart, uart_char_obj, job, first_err):
        """On a NotPermitted/Not-paired refusal: bond via BlueZ inside the
        open GATT session, then retry the job once. Raises a BleUnavailable
        whose text carries the pairing-mode instruction when auto-pairing
        does not land the bond (that text becomes the user-visible retry
        banner detail)."""
        _LOGGER.warning(
            "Bed %s refused the write (not paired) — auto-pairing via BlueZ now; "
            "if it fails, put the bed in pairing mode before the next attempt",
            self.address,
        )
        try:
            await self._async_pair_via_bluez()
        except Exception as pair_err:  # noqa: BLE001
            raise BleUnavailable(
                f"Controller requires pairing and automatic BlueZ pairing failed "
                f"({pair_err}). Put the bed in pairing mode (hold the pairing/"
                f"Bluetooth button on the base ~5 s until it indicates pairing), "
                f"then reload the integration."
            ) from first_err
        _LOGGER.info(
            "Retrying %s ASCII command after pairing", self.address
        )
        try:
            return await job(_AsciiIO(client, uart_char_obj if uart_char_obj is not None else uart))
        except Exception as retry_err:  # noqa: BLE001
            if self._is_not_paired_error(retry_err):
                # Bond exists but the bed still refuses (e.g. link did not
                # re-encrypt, or the bed wants an explicit pairing window) —
                # surface the pairing-mode instruction in the retry banner.
                raise BleUnavailable(
                    f"Bed is bonded but still refuses the ASCII write "
                    f"({retry_err}). Put the bed in pairing mode (hold the "
                    f"pairing/Bluetooth button on the base ~5 s until it "
                    f"indicates pairing), then reload the integration."
                ) from retry_err
            raise


    async def _ascii_session(self, job):
        """Open one GATT session, run a job(client), answer via ASCII plane."""
        from bleak_retry_connector import establish_connection

        device = self._ble_device()
        if device is None:
            raise BleUnavailable(f"{self.address} not in range / no proxy")
        async with self._lock:
            client = await establish_connection(
                __import__("bleak").BleakClient, device, self.address
            )
            try:
                # The UART char differs by radio generation: the 360-gen
                # controller (fuzion service 09d23fae) exposes 421e00f3…; the
                # BAM pump hub (ffffd1fd) uses efee2. Resolve from the live
                # service table so one client class serves either radio.
                # bleak exposes the collection either as a property (recent
                # versions) or via awaitable get_services() (older); try both.
                # If neither yields the fuzion char, dump the table once — that
                # dump answers "connected to what?" with zero guessing.
                uart = RX_UUID
                uart_char_obj = None
                services = None
                with contextlib.suppress(Exception):
                    services = await client.get_services()
                if not services:
                    services = getattr(client, "services", None)
                table: list[str] = []
                services_found = False
                try:
                    for service in services or []:
                        services_found = True
                        for char in service.characteristics:
                            # bleak's properties = a plain set of strings
                            # ("read"/"write"/...) — not objects with .name.
                            table.append(
                                f"{char.uuid} props={sorted(char.properties)}"
                            )
                            if char.uuid.lower().startswith("421e00f3"):
                                uart = char.uuid
                                uart_char_obj = char
                                break
                        if uart_char_obj is not None:
                            break
                except (TypeError, AttributeError) as err:
                    _LOGGER.debug("service iteration shape unexpected: %s", err)
                if uart_char_obj is not None:
                    _LOGGER.debug("ASCII UART char resolved from the device table")
                    try:
                        return await job(_AsciiIO(client, uart_char_obj))
                    except Exception as err:  # noqa: BLE001
                        if self._is_not_paired_error(err):
                            return await self._pair_and_retry(
                                client, uart, uart_char_obj, job, err
                            )
                        raise
                if services_found:
                    _LOGGER.warning(
                        "421e00f3 UART char NOT in the service table of %s — got: %s",
                        self.address, table,
                    )
                else:
                    _LOGGER.warning(
                        "No service table could be read from %s (get_services and "
                        "the services property both empty) — ASCII probe will fail",
                        self.address,
                    )
                try:
                    return await job(_AsciiIO(client, uart))
                except Exception as err:  # noqa: BLE001
                    if self._is_not_paired_error(err):
                        return await self._pair_and_retry(
                            client, uart, uart_char_obj, job, err
                        )
                    raise
            finally:
                await client.disconnect()

    async def _ascii_command(self, cmd: str, **kw) -> str:
        async def job(io):
            return await io.command(cmd, **kw)
        return await self._ascii_session(job)

    async def ascii_dashboard(self) -> list[str]:
        """One BAMG batch — every dashboard state in one round trip."""
        frame = bamkey_ascii.build_ascii_frame(
            "BAMG " + json.dumps(
                [{"bamkey": k, "args": a} for k, a in self.ASCII_BATCH],
                separators=(",", ":"),
            )
        )
        async def job(io):
            return await io.command_prebuilt(frame)
        return await self._ascii_session(job)

    async def ascii_set_sleep_number(self, side: str, value: int) -> None:
        """PSNS <side> <value>, then poll SNCG until the pump settles."""
        async def job(io):
            await io.command(f"PSNS {side} {int(value)}")
            deadline = asyncio.get_event_loop().time() + 120
            while True:
                await asyncio.sleep(1.2)
                try:
                    val = await io.command(f"SNCG {side}")
                except Exception:  # noqa: BLE001 — keep polling through hiccups
                    continue
                if val.split() and val.split()[0] == "0":
                    return
                if asyncio.get_event_loop().time() > deadline:
                    raise TimeoutError(f"{side} did not settle")
        await self._ascii_session(job)

    async def ascii_preset(self, side: str, name: str) -> None:
        """ASTP <side> <name> — bed-level (single-motor base moves both sides).

        Verified names: "snore", "flat". Unverified repo option names get
        candidate fallbacks; a FAIL:<code> reply raises and the caller keeps its
        previous state rather than silently mis-setting the base.
        """
        candidates = self.ASCII_PRESET_CANDIDATES.get(name, [name])
        last: Exception | None = None
        for word in candidates:
            try:
                await self._ascii_command(f"ASTP {side} {word}")
                return
            except Exception as err:  # noqa: BLE001 — try the next spelling
                last = err
        raise last or TimeoutError(f"no candidate accepted for preset {name!r}")

    ASCII_PRESET_CANDIDATES = {
        "flat": ["flat"],
        "snore": ["snore"],
        "read": ["read"],
        "watch_tv": ["watch_tv", "tv", "watch tv"],
        "zero_g": ["zero_g", "zero g", "zerog"],
    }

    async def ascii_light(self, level: str, minutes: int = 0) -> None:
        await self._ascii_command(f"UBLS {level} {int(minutes)}")

    async def ascii_light_auto(self, enabled: bool, level: str = "low") -> None:
        await self._ascii_command(f"UBAS {'true' if enabled else 'false'} {level}")

    async def ascii_foot_warm(self, side: str, level: str, minutes: int = 120) -> None:
        await self._ascii_command(f"FWTS {side} {level} {int(minutes)}")

    async def ascii_time_sync(self, iso: str) -> None:
        await self._ascii_command(f"SYTS {iso}")

    # ------------------------------------------------------------------
    # ASCII-mode lifecycle: detection, snapshot, baseline learning.
    # ------------------------------------------------------------------

    mode: str = "unknown"  # "ascii" | "mcr" | "unknown"
    baselines: dict = {}   # side → rolling empty-bed pressure (learned in-memory)

    async def detect_ascii(self) -> bool:
        """One cheap probe: UBLG answers PASS on the ASCII plane.

        Records the mode so the coordinator can prefer ASCII and still fall
        back to the binary MCR path (older BAM pump firmware) when this hub
        generation is not present. Failures are logged once per probe so a
        wedged retry loop is diagnosable from the HA log.
        """
        try:
            await self._ascii_command("UBLG")
        except Exception as err:  # noqa: BLE001 — no ASCII plane here: fall back to MCR
            _LOGGER.warning(
                "ASCII probe to %s failed (%s); trying legacy MCR",
                self.address, err,
            )
            import traceback
            _LOGGER.warning(
                "ASCII probe failure detail (traceback above the warning is "
                "the integration's pairing/GATT work): %s",
                traceback.format_exc(),
            )
            self.mode = "mcr"
            return False
        self.mode = "ascii"
        return True

    async def ascii_snapshot(self) -> "bamkey_ascii.AsciiSnapshot":
        """One BAMG batch → decoded snapshot; learns empty-baselines per side.

        Baselines are slow minima: when a side is NOT adjusting and its pressure
        is the lowest seen since process start, that becomes the rolling
        baseline. Presence derives from baseline + threshold elsewhere; the
        learning resets on integration reload (documented in NOTES).
        """
        snapshot = bamkey_ascii.snapshot_from_batch(await self.ascii_dashboard())
        for side, state in (("left", snapshot.sn_left), ("right", snapshot.sn_right)):
            if state is None or state.adjusting:
                continue
            known = self.baselines.get(side)
            if known is None or state.pressure < known:
                self.baselines[side] = state.pressure
        return snapshot


class _AsciiIO:
    """Thin write/read-back wrapper used inside one established GATT session."""

    def __init__(self, client, uart_char: str) -> None:
        self.client = client
        self.uart_char = uart_char

    async def command(self, cmd: str, **kwargs) -> str:
        frame = bamkey_ascii.build_ascii_frame(cmd)
        # Bounded write: a busy/one-connection controller (phone app or scanner
        # holding the GATT slot) makes the ATT write hang forever — time it out
        # so the coordinator surfaces "timed out" instead of wedging setup.
        async with asyncio.timeout(20):
            await self.client.write_gatt_char(self.uart_char, frame, response=True)
        await asyncio.sleep(0.05)
        for _ in range(12):
            try:
                async with asyncio.timeout(8):
                    resp = await self.client.read_gatt_char(self.uart_char)
            except (TimeoutError, Exception):  # noqa: BLE001 — transient; retry inside window
                resp = b""
            frames = bamkey_ascii.parse_ascii_frames(resp or b"")
            if frames:
                return bamkey_ascii.parse_response(frames[0])
            await asyncio.sleep(0.08)
        raise TimeoutError(f"no ASCII response to {cmd!r}")

    async def command_prebuilt(self, frame: bytes) -> list[str]:
        """Write a pre-built frame (e.g. a big BAMG batch); read the response."""
        async with asyncio.timeout(25):
            await self.client.write_gatt_char(self.uart_char, frame, response=True)
        await asyncio.sleep(0.1)
        for _ in range(15):
            try:
                async with asyncio.timeout(8):
                    resp = await self.client.read_gatt_char(self.uart_char)
            except (TimeoutError, Exception):  # noqa: BLE001
                resp = b""
            frames = bamkey_ascii.parse_ascii_frames(resp or b"")
            if frames:
                return bamkey_ascii.parse_batch(frames[0])
            await asyncio.sleep(0.08)
        raise TimeoutError("no ASCII batch response")


class BleUnavailable(Exception):
    """Raised when the bed cannot be reached over BLE."""
