"""Foot warmer (heated foot area) per-side for Sleep Number Local-First.

Wire-verified control: `FWTS <side> <off|low|medium|high> <minutes>` — the
response JSON (`FWTG left` → PASS:{"right":null,"left":null}) reports each side
independently, so this is a per-side select, one per sleeper. The timer is the
hub's own default (120 min, the app's value); v1 keeps that fixed and exposes
the state honestly.
"""

from __future__ import annotations

import json
import logging

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ._compat import AddConfigEntryEntitiesCallback
from .coordinator import SleepNumberConfigEntry
from .entity import sleeper_device_info
from .sleepiq_local.consts import Side

_LOGGER = logging.getLogger(__name__)

FW_OPTIONS = ("off", "low", "medium", "high")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SleepNumberConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up per-side foot-warmer selects when the ASCII plane is present."""
    data = entry.runtime_data
    ble = data.ble
    if not data.capabilities.get("ascii") or ble is None:
        return
    entities = [
        FootWarmerSelect(data.settings, entry, ble, bed, sleeper)
        for bed in data.client.beds.values()
        for sleeper in bed.sleepers
    ]
    async_add_entities(entities)


class FootWarmerSelect(CoordinatorEntity, SelectEntity):
    """Foot warmer level for one side of the bed."""

    _attr_has_entity_name = True
    _attr_translation_key = "foot_warmer"
    _attr_icon = "mdi:heat-wave"
    _attr_options = list(FW_OPTIONS)

    def __init__(self, coordinator, entry, ble, bed, sleeper) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._ble = ble
        self.bed = bed
        self.sleeper = sleeper
        self._word = "left" if sleeper.side == Side.LEFT else "right"
        self._attr_device_info = sleeper_device_info(
            coordinator.hass, coordinator.config_entry.entry_id, bed, sleeper
        )
        self._attr_unique_id = f"{sleeper.sleeper_id}_foot_warmer"
        self._attr_current_option = "off"

    @callback
    def _handle_coordinator_update(self) -> None:
        level = (getattr(self.coordinator, "fw_state", {}) or {}).get(self._word)
        if level in FW_OPTIONS:
            self._attr_current_option = level
        self.async_write_ha_state()
        super()._handle_coordinator_update()

    async def async_select_option(self, option: str) -> None:
        await self._ble.ascii_foot_warm(self._word, option)
        self._attr_current_option = option
        self.async_write_ha_state()
