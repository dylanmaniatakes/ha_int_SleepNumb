"""Base-position preset selector, driven over BLE.

This exists because the cloud has severed the foundation relay ("No Foundation
Device"), yet the base is fully reachable over the hub's BLE radio. Selecting a
preset sends an MCR foundation command straight to the bed — recovering base
control the cloud gave up.
"""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant, HomeAssistantError, callback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import mcr
from ._compat import AddConfigEntryEntitiesCallback
from .const import BASE_PRESET
from .coordinator import SleepNumberConfigEntry
from .entity import sleeper_device_info
from .sleepiq_local.consts import Side

# Human labels -> MCR preset codes (per-sleeper side).
PRESET_OPTIONS = {
    "flat": mcr.PRESETS["flat"],
    "zero_g": mcr.PRESETS["zero_g"],
    "read": mcr.PRESETS["read"],
    "watch_tv": mcr.PRESETS["watch_tv"],
    "snore": mcr.PRESETS["snore"],
    "favorite": mcr.PRESETS["favorite"],
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SleepNumberConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up base-preset selects when a BLE transport is available."""
    data = entry.runtime_data
    if not data.capabilities.get("ble") or data.ble is None:
        return
    entities = [
        BasePresetSelect(data.status, bed, sleeper, data.ble)
        for bed in data.client.beds.values()
        for sleeper in bed.sleepers
    ]
    async_add_entities(entities)


class BasePresetSelect(CoordinatorEntity, SelectEntity):
    """Activate a foundation preset for one sleeper's side over BLE."""

    _attr_has_entity_name = True
    _attr_translation_key = BASE_PRESET
    _attr_icon = "mdi:bed"
    _attr_options = list(PRESET_OPTIONS)

    def __init__(self, coordinator, bed, sleeper, ble) -> None:
        super().__init__(coordinator)
        self.bed = bed
        self.sleeper = sleeper
        self._ble = ble
        self._attr_device_info = sleeper_device_info(
            coordinator.hass, coordinator.config_entry.entry_id, bed, sleeper
        )
        self._attr_unique_id = f"{sleeper.sleeper_id}_{BASE_PRESET}"
        self._attr_current_option = None

    @callback
    def _handle_coordinator_update(self) -> None:
        # Reflect the last activated preset if the foundation status reports one.
        super()._handle_coordinator_update()

    async def async_select_option(self, option: str) -> None:
        side = mcr.SIDE_LEFT if self.sleeper.side == Side.LEFT else mcr.SIDE_RIGHT
        side_word = "left" if self.sleeper.side == Side.LEFT else "right"
        if getattr(self._ble, "mode", None) == "ascii":
            # 360-gen controller: presets are bed-level words (single-motor base
            # moves both sides); "favorite" = recall the stored sleep number.
            if option == "favorite":
                await self.coordinator.async_request_refresh()
                snap = getattr(self.coordinator, "_ascii_snap", None)
                fav = (snap.fav_left if side_word == "left" else snap.fav_right) if snap else None
                if fav is None:
                    raise HomeAssistantError("favorite sleep number not known yet")
                await self._ble.ascii_set_sleep_number(side_word, fav)
            else:
                await self._ble.ascii_preset(side_word, option)
        else:
            preset = PRESET_OPTIONS[option]
            await self._ble.activate_preset(side, preset)
        self._attr_current_option = option
        self.async_write_ha_state()
