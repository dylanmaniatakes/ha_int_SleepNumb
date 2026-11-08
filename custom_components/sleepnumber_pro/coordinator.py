"""Data update coordinators for Sleep Number (SleepIQ) Local-First.

Design goal from the audit: never let one severed subsystem take the whole
integration down.  Sleep Number is decommissioning device connectivity feature
by feature, so every fetch degrades independently — a foundation that the cloud
now reports as absent must not blank out the live bed-sensor entities.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .bluetooth import BleBridgeClient
from .bamkey_ascii import AsciiSnapshot, presence_from_pressure
from .const import (
    DOMAIN,
    SETTINGS_INTERVAL,
    SLEEP_DATA_INTERVAL,
    SOURCE_CLOUD,
    SOURCE_LOCAL,
    STATUS_INTERVAL,
)
from .local import LocalBridgeClient, LocalStatus
from .sleepiq_local import (
    AsyncSleepIQ,
    SleepIQAPIException,
    SleepIQConnectionException,
    SleepIQLoginException,
    SleepIQTimeoutException,
)
from .sleepiq_local.consts import Side

_LOGGER = logging.getLogger(__name__)

# Errors the cloud can raise mid-fetch. Transient ones retry; the rest go through _reraise.
_TRANSIENT = (SleepIQTimeoutException, SleepIQConnectionException)
_FETCH_ERRORS = (SleepIQLoginException, SleepIQAPIException, *_TRANSIENT)

type SleepNumberConfigEntry = ConfigEntry[SleepNumberData]


@dataclass
class SleepNumberData:
    """Runtime data stored on the config entry."""

    client: AsyncSleepIQ
    status: SleepNumberStatusCoordinator
    settings: SleepNumberSettingsCoordinator
    sleep: SleepNumberSleepDataCoordinator
    # Capability flags discovered at setup, surfaced to platforms.
    capabilities: dict[str, bool] = field(default_factory=dict)
    # BLE transport, if a bed BLE address is configured (drives base presets).
    ble: object | None = None
    # Last decoded ASCII-plane snapshot (None until the controller answers a
    # BAMG batch). Platforms read per-feature state from here.
    last_snapshot: AsciiSnapshot | None = None


class _BaseCoordinator(DataUpdateCoordinator[None]):
    """Shared auth-aware error handling."""

    config_entry: SleepNumberConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: SleepNumberConfigEntry,
        client: AsyncSleepIQ,
        name: str,
        interval,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{entry.data[CONF_USERNAME]}@{DOMAIN}:{name}",
            update_interval=interval,
        )
        self.client = client

    def _reraise(self, err: Exception) -> None:
        """Translate library errors into coordinator outcomes."""
        if isinstance(err, SleepIQLoginException):
            raise ConfigEntryAuthFailed(str(err)) from err
        if isinstance(err, SleepIQTimeoutException):
            raise UpdateFailed(f"Timed out talking to SleepIQ: {err}") from err
        if isinstance(err, SleepIQConnectionException):
            raise UpdateFailed(f"Cannot reach SleepIQ: {err}") from err
        if isinstance(err, SleepIQAPIException):
            if err.code == 401:
                raise ConfigEntryAuthFailed from err
            raise UpdateFailed(f"SleepIQ API error {err.code}: {err}") from err
        raise UpdateFailed(str(err)) from err


class SleepNumberStatusCoordinator(_BaseCoordinator):
    """Live bed-sensor plane: presence, sleep number, pressure (+ foundation if present).

    Local-first: when an on-hub bridge is configured and answering, presence and
    sleep number come straight from the hub (no cloud). If the bridge is
    unreachable, this transparently falls back to the cloud for that cycle. The
    active transport is exposed as ``source`` for the connection sensor.
    """

    def __init__(
        self,
        hass,
        entry,
        client,
        local: LocalBridgeClient | BleBridgeClient | None = None,
    ) -> None:
        super().__init__(hass, entry, client, "status", STATUS_INTERVAL)
        self.local = local
        self.source = SOURCE_CLOUD

    async def _async_update_data(self) -> None:
        # Prefer a local transport (BLE or on-hub bridge) when configured.
        if self.local is not None:
            try:
                snapshot = await self._local_update()
                if snapshot is not None:
                    self._apply_ascii(snapshot)
                self.source = getattr(self.local, "source_name", SOURCE_LOCAL)
                return
            except Exception as err:  # noqa: BLE001 - any local failure -> cloud
                _LOGGER.debug("Local transport unavailable, using cloud: %s", err)

        # Cloud fallback.
        self.source = SOURCE_CLOUD
        try:
            await self.client.fetch_bed_statuses()
        except _FETCH_ERRORS as err:
            self._reraise(err)

        # Foundation is optional and may be severed at any time — best-effort only.
        for bed in self.client.beds.values():
            if not bed.foundation.type:
                continue
            try:
                await bed.foundation.update_foundation_status()
            except (SleepIQAPIException, *_TRANSIENT) as err:
                _LOGGER.debug("Foundation status unavailable for %s: %s", bed.name, err)

    async def _local_update(self) -> AsciiSnapshot | None:
        """Run the local transport; return an ASCII snapshot when that plane
        answered, None for legacy MCR/bridge snapshots (handled by _apply_local)."""
        local = self.local
        if local is None:
            return None
        if not isinstance(local, BleBridgeClient):
            # On-hub bridge (UART root path): legacy LocalStatus plane.
            self._apply_local(await local.status())
            return None
        # BLE: prefer the ASCII bamkey plane (360-gen controller); fall back to
        # the binary MCR pump plane and then cloud, degrading feature by feature.
        if local.mode == "unknown":
            await local.detect_ascii()
        if local.mode == "ascii":
            snap = await local.ascii_snapshot()
            self._apply_ascii(snap)
            return snap
        self._apply_local(await local.status())
        return None

    def _apply_ascii(self, snap: AsciiSnapshot) -> None:
        """Merge an ASCII snapshot: publish it for the new platforms and apply
        sleep numbers / pressure / pressure-derived presence to the sleepers.

        Presence here is ``bamkey_ascii.presence_from_pressure`` — the local
        signal the cloud approximates server-side (LBPG is not served: FAIL:2).
        Unknown components keep their previous value (never blank a subsystem).
        """
        # Platform setup runs after the first refresh; stash on the coordinator
        # first so entities always see it, then mirror into runtime data.
        self._ascii_snap = snap
        try:
            self.config_entry.runtime_data.last_snapshot = snap
        except (AttributeError, RuntimeError):
            pass
        sides = {
            Side.LEFT: ("left", snap.sn_left, snap.fav_left),
            Side.RIGHT: ("right", snap.sn_right, snap.fav_right),
        }
        for bed in self.client.beds.values():
            baselines = getattr(self.local, "baselines", {})
            for sleeper in bed.sleepers:
                name, st, fav = sides.get(sleeper.side, (None, None, None))
                if st is not None:
                    if not st.adjusting:
                        sleeper.sleep_number = st.sleep_number
                    sleeper.pressure = st.pressure
                    present = presence_from_pressure(st.pressure, baselines.get(name))
                    if present is not None:
                        sleeper.in_bed = present
                if fav is not None:
                    sleeper.fav_sleep_number = fav

    def _apply_local(self, snap: LocalStatus) -> None:
        """Merge a local bridge snapshot into the bed/sleeper objects.

        The bridge covers the pump plane (presence + sleep number). Cloud-only
        fields such as raw pressure keep their last value until a cloud cycle.
        """
        for bed in self.client.beds.values():
            for sleeper in bed.sleepers:
                if sleeper.side == Side.LEFT:
                    if snap.in_bed_left is not None:
                        sleeper.in_bed = snap.in_bed_left
                    if snap.sleep_number_left is not None:
                        sleeper.sleep_number = snap.sleep_number_left
                elif sleeper.side == Side.RIGHT:
                    if snap.in_bed_right is not None:
                        sleeper.in_bed = snap.in_bed_right
                    if snap.sleep_number_right is not None:
                        sleeper.sleep_number = snap.sleep_number_right


class SleepNumberSettingsCoordinator(_BaseCoordinator):
    """Slow-changing config: privacy pause and Responsive Air."""

    fw_state: dict[str, str | None] = {}  # side → foot-warm level word (ASCII)

    def __init__(self, hass, entry, client, ble=None) -> None:
        super().__init__(hass, entry, client, "settings", SETTINGS_INTERVAL)
        self._ble_ref = ble
        self.fw_state = {}

    async def _async_update_data(self) -> None:
        for bed in self.client.beds.values():
            try:
                await bed.fetch_pause_mode()
            except _TRANSIENT as err:
                self._reraise(err)
            except SleepIQAPIException as err:
                if err.code == 401:
                    self._reraise(err)
                _LOGGER.debug("Pause mode unavailable for %s: %s", bed.name, err)
            try:
                await bed.fetch_responsive_air()
            except _TRANSIENT as err:
                self._reraise(err)
            except SleepIQAPIException as err:
                _LOGGER.debug("Responsive Air unavailable for %s: %s", bed.name, err)
        # ASCII plane (360-gen controller): foot-warm state lives here; the fast
        # status coordinator doesn't poll it, so this slow lane does.
        ble = getattr(self, "_ble_ref", None)
        if ble is not None and getattr(ble, "mode", None) == "ascii":
            fw: dict[str, str | None] = {}
            for side in ("left", "right"):
                try:
                    raw = await ble._ascii_command(f"FWTG {side}")
                    state = json.loads(raw)
                    own = state.get(side) if isinstance(state, dict) else None
                    fw[side] = own.split()[0] if isinstance(own, str) and own.split() else "off"
                except Exception as err:  # noqa: BLE001 — slow lane, keep last value
                    _LOGGER.debug("FWTG %s unavailable: %s", side, err)
                    fw.setdefault(side, None)
            self.fw_state = fw if fw else getattr(self, "fw_state", {})


class SleepNumberSleepDataCoordinator(_BaseCoordinator):
    """Nightly sleep-health record (heart rate, respiration, HRV, score)."""

    def __init__(self, hass, entry, client) -> None:
        super().__init__(hass, entry, client, "sleep", SLEEP_DATA_INTERVAL)

    async def _async_update_data(self) -> None:
        tasks = [
            sleeper.fetch_sleep_data()
            for bed in self.client.beds.values()
            for sleeper in bed.sleepers
        ]
        try:
            await asyncio.gather(*tasks)
        except _FETCH_ERRORS as err:
            self._reraise(err)
