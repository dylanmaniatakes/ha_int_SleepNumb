"""ASCII bamkey UART client for Sleep Number 360-gen hubs (fUzIoN framing).

This is the LIVE protocol captured byte-exact from a 360-gen hub on 2026-11-08
(Dylan's bed; see RE workspace bamkey_reference.md for the wire grammar and the
full 158-key vocabulary). The binary MCR protocol in ``mcr.py`` remains for
older BAM pump firmware; hubs that answer the ASCII plane prefer this client.

Wire format (verified):
    frame = "fUzIoN" + u32 LE (TOTAL frame length, sync+4+payload included)
            + payload + CRC32 LE (over sync + u32 + payload)
    payload = ascii command, e.g.  "UBLS off 0", "PSNS right 40", "BAMG [...]"
    response = read the SAME UART characteristic: a response frame whose payload
    is "PASS:" + value (bare ack / single value / JSON / JSON-array for BAMG).

Transport: one GATT characteristic handles write + read (+ notify). The app
writes the command, then reads the response back from the same char.
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
import zlib
from dataclasses import dataclass, field

_LOGGER = logging.getLogger(__name__)

FUZION_SYNC = b"fUzIoN"


def build_ascii_frame(command: str) -> bytes:
    """Frame an ASCII bamkey command.

    Length field = TOTAL frame size INCLUDING sync, the length field itself AND the
    trailing CRC32 (wire-verified: "PSNS right 40" = 27 = 10 + 13 + 4). CRC32 is
    computed over sync + u32 + payload, stored little-endian (256/256 captured
    frames verify).
    """
    payload = command.encode("ascii")
    total = len(FUZION_SYNC) + 4 + len(payload) + 4
    head = FUZION_SYNC + struct.pack("<I", total) + payload
    return head + struct.pack("<I", zlib.crc32(head) & 0xFFFFFFFF)


def parse_ascii_frames(data: bytes) -> list[str]:
    """Parse every fUzIoN frame in a read buffer; return payload strings."""
    out = []
    i = 0
    while True:
        i = data.find(FUZION_SYNC, i)
        if i < 0 or i + 14 > len(data):
            break
        total = struct.unpack("<I", data[i + 6:i + 10])[0]
        if total < 14:
            i += 1
            continue
        chunk = data[i + 10:i + total]
        if len(chunk) < 4:
            break
        got = struct.unpack("<I", chunk[-4:])[0]
        calc = zlib.crc32(data[i:i + 10] + chunk[:-4]) & 0xFFFFFFFF
        if got != calc:
            _LOGGER.debug("ascii frame crc mismatch: %s", chunk[:-4])
            i += 1
            continue
        try:
            out.append(chunk[:-4].decode("ascii"))
        except Exception:  # noqa: BLE001 - non-ascii payload, skip frame
            _LOGGER.debug("ascii frame non-decodable: %s", chunk[:-4])
        i += total
    return out


@dataclass
class SleepNumberState:
    """Parsed SNCG reply: PASS:<state> <sleep_number> <pressure>."""
    adjusting: bool
    sleep_number: int
    pressure: int


@dataclass
class BatchAnswers:
    """Parsed BAMG batch reply — query order → value strings."""
    values: list[str] = field(default_factory=list)

    def for_key(self, key: str) -> str | None:
        """Look up the response for a bamkey by re-querying the (fixed) order."""
        return self.values[getattr(self, "_order", []).index(key)] if key in getattr(self, "_order", []) else None


def parse_response(payload: str) -> str:
    """Strip the PASS: envelope; caller dispatches on shape.

    "PASS:"           -> ""  (set ack)
    "PASS:<v>"        -> "<v>"  (single get / status)
    "PASS:[...]"      -> JSON array of per-query PASS strings (BAMG batch)
    """
    if not payload.startswith("PASS:"):
        raise ValueError(f"unexpected bed response: {payload[:60]!r}")
    return payload[5:]


def parse_sncg(value: str):
    """SNCG reply 'state sn pressure' → SleepNumberState."""
    state, sn, pressure = value.split()
    return SleepNumberState(
        adjusting=state != "0", sleep_number=int(sn), pressure=int(pressure)
    )


def build_batch(query: list[tuple[str, str]]) -> str:
    """BAMG batch command: JSON array of {"bamkey":..,"args":..}."""
    entries = [{"bamkey": k, "args": a} for k, a in query]
    return "BAMG " + json.dumps(entries, separators=(",", ":"))


def parse_batch(value: str) -> list[str]:
    """BAMG response → list of per-query values (query order).

    Accepts either the raw response payload ("PASS:[...json array...]") or the
    already-stripped array text; single-value responses pass through unchanged.
    """
    if value.startswith("PASS:"):
        value = value[5:]
    if value.startswith("["):
        return [parse_response(v) for v in json.loads(value)]
    return [value]


# ---------------------------------------------------------------------------
# Snapshot mapping: BAMG batch (fixed live order) → typed state per bed.
# Query order verified on the wire 2026-11-08; the bed answers a JSON array of
# PASS strings in the SAME order, so mapping is positional.
# ---------------------------------------------------------------------------

BAMG_ORDER: list[tuple[str, str, str]] = [
    # (bamkey, args, semantic slot)
    ("UBLG", "", "light"),
    ("SNCG", "left", "sn_left"),
    ("SNFG", "left", "fav_left"),
    ("ACTG", "left head", "pos_head"),
    ("ACTG", "left foot", "pos_foot"),
    ("ACGP", "left", "progress"),
    ("AGCP", "left", "preset"),
    ("UBAG", "", "light_auto"),
    ("SNCG", "right", "sn_right"),
    ("SNFG", "right", "fav_right"),
    ("ACTG", "right head", "pos_head"),   # same joint base — values mirror
    ("ACTG", "right foot", "pos_foot"),
    ("ACGP", "right", "progress"),
    ("AGCP", "right", "preset_last"),
]

SNCG_SLOTS = ("sn_left", "sn_right")


@dataclass
class AsciiSnapshot:
    """One BAMG batch round trip, decoded into per-feature values."""

    sn_left: SleepNumberState | None = None
    sn_right: SleepNumberState | None = None
    fav_left: int | None = None
    fav_right: int | None = None
    pos_head: int | None = None          # degrees (shared single-motor base)
    pos_foot: int | None = None
    progress: str | None = None          # ACGP: "none <steps-remaining>"
    preset: str | None = None            # AGCP-left: in_progress | none | name
    preset_last: str | None = None       # AGCP-right: last-used preset name
    light: str | None = None             # UBLG: "low <minutes>"
    light_auto: str | None = None        # UBAG: "off <level> <minutes>"
    error: str | None = None             # first FAIL:<code> seen in the batch


def snapshot_from_batch(values: list[str]) -> AsciiSnapshot:
    """Map ordered BAMG responses onto an AsciiSnapshot.

    Tolerates short batches (missing trailing entries stay None) and FAIL codes
    (first failure recorded on ``error``; other slots keep parsing — a severed
    subsystem never blanks the rest, matching the coordinator's design goal).
    """
    snap = AsciiSnapshot()
    if not values:
        snap.error = "empty batch response"
        return snap
    for slot, raw in zip(BAMG_ORDER, values):
        name = slot[2]
        if raw.startswith("FAIL:"):
            if snap.error is None:
                snap.error = raw
            continue
        try:
            if name in SNCG_SLOTS:
                setattr(snap, name, parse_sncg(raw))
            elif name in ("fav_left", "fav_right", "pos_head", "pos_foot"):
                setattr(snap, name, int(raw))
            else:
                setattr(snap, name, raw)
        except (ValueError, IndexError):
            if snap.error is None:
                snap.error = f"unparsed {name}: {raw[:40]!r}"
    return snap


def presence_from_pressure(pressure: int | None, baseline: int | None,
                           delta: int = 6) -> bool | None:
    """Local presence: pressure above the learned empty-baseline.

    The cloud's "detection" is server-side inference from exactly this signal
    (LBPG is FAIL:2 — not served). ``baseline`` is the rolling empty-bed
    pressure; any value above baseline+delta reads occupied. Returns None when
    either input is unknown.
    """
    if pressure is None or baseline is None:
        return None
    return pressure - baseline > delta


class BamkeyAsciiClient:
    """BLE client speaking the ASCII bamkey plane via a Bleak GATT device.

    The caller supplies connect/get_client() via a bleak BleakClient coroutine;
    this class does framing, read-back and response parsing only. Kept transport-
    thin so tests can run it against recorded frames.
    """

    uart_char = BLE_UART = None  # set by the BLE wiring (bluetooth.py) at init

    def __init__(self, client, uart_char: str) -> None:
        self.client = client
        self.uart_char = uart_char
        self._lock = asyncio.Lock()

    async def command(self, cmd: str, *, settle: int = 0) -> str:
        """Write a command and read the response from the same characteristic."""
        async with self._lock:
            await self.client.write_gatt_char(self.uart_char, build_ascii_frame(cmd), response=True)
            await asyncio.sleep(0.05)
            for _ in range(12):
                try:
                    resp = await self.client.read_gatt_char(self.uart_char)
                except Exception:  # noqa: BLE001 — transient read, retry inside window
                    resp = b""
                frames = parse_ascii_frames(resp or b"")
                if frames:
                    return parse_response(frames[0])
                await asyncio.sleep(0.08)
            raise TimeoutError(f"no response to {cmd!r}")

    async def command_batch(self, query: list[tuple[str, str]]) -> list[str]:
        """Run a BAMG multi-query and return the ordered response list."""
        resp = await self.command(build_batch(query))
        return parse_batch(resp if not resp.startswith("PASS:") else resp)

    # ---- high-level controls (all wire-verified) ----

    async def get_sleep_number(self, side: str) -> SleepNumberState | None:
        """SNCG <side> — includes live pressure + adjusting flag."""
        val = await self.command(f"SNCG {side}")
        try:
            return parse_sncg(val)
        except ValueError:
            return None

    async def wait_sleep_number(self, side: str, timeout: float = 60.0) -> SleepNumberState:
        """Poll SNCG until the pump settles (state 0) or timeout."""
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            st = await self.get_sleep_number(side)
            if st and not st.adjusting:
                return st
            await asyncio.sleep(1.2)
        raise TimeoutError(f"{side} sleep number did not settle in {timeout}s")

    async def set_sleep_number(self, side: str, value: int) -> None:
        """PSNS <side> <value> then wait for settle."""
        await self.command(f"PSNS {side} {int(value)}")
        await self.wait_sleep_number(side)

    async def get_favorite(self, side: str) -> int | None:
        val = await self.command(f"SNFG {side}")
        try:
            return int(val)
        except ValueError:
            return None

    async def set_favorite(self, side: str, value: int) -> None:
        await self.command(f"SNFS {side} {int(value)}")

    async def set_preset(self, side: str, name: str) -> None:
        """ASTP <side> <preset-name> — name from AGCP vocabulary
        (flat, read, watch_tv→? watch tv as 'tv'? see docs; snore confirmed)."""
        await self.command(f"ASTP {side} {name}")

    async def get_preset_state(self, side: str) -> str:
        """AGCP <side>: in_progress | none | <preset-name>."""
        return await self.command(f"AGCP {side}")

    async def get_actuator(self, side: str, part: str) -> int | None:
        """ACTG <side> <head|foot> → degrees."""
        val = await self.command(f"ACTG {side} {part}")
        try:
            return int(val)
        except ValueError:
            return None

    async def foot_warm(self, side: str, level: str, minutes: int = 120) -> None:
        """FWTS <side> <off|low|medium|high> <minutes>."""
        await self.command(f"FWTS {side} {level} {int(minutes)}")

    async def foot_warm_state(self, side: str) -> str:
        """FWTG <side> → JSON {"right":...,"left":...} or level string."""
        return await self.command(f"FWTG {side}")

    async def set_light(self, level: str, minutes: int = 0) -> None:
        """UBLS <off|low|medium|high> <minutes>."""
        await self.command(f"UBLS {level} {int(minutes)}")

    async def set_light_auto(self, enabled: bool, level: str = "low") -> None:
        """UBAS <true|false> <level>."""
        await self.command(f"UBAS {'true' if enabled else 'false'} {level}")

    async def light_state(self) -> str:
        """UBLG → 'PASS:low 0'-style value."""
        return await self.command("UBLG")

    async def sync_time(self, iso: str) -> None:
        """SYTS <ISO-8601> — the app does this every few minutes; once after connect."""
        await self.command(f"SYTS {iso}")
