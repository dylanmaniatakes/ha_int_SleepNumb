"""Under-bed light for Sleep Number Local-First (ASCII bamkey plane).

The controller's `UBLS <level> <minutes>` carries no side argument — the light
is one shared fixture for the bed, so this is ONE light entity (not per-side).
Levels are words on the wire (off/low/medium/high); mapped onto HA brightness
so a dimmer control works naturally.

Requires the ASCII plane: the entity only registers when the 360-gen
controller answered detection (see coordinator/manifest ble capability).
"""

from __future__ import annotations

import logging

from homeassistant.components.light import (
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.core import HomeAssistant, callback

from .bamkey_ascii import AsciiSnapshot
from ._compat import AddConfigEntryEntitiesCallback
from .coordinator import SleepNumberConfigEntry

_LOGGER = logging.getLogger(__name__)

LEVEL_WORDS = ("off", "low", "medium", "high")
LEVEL_TO_BRIGHTNESS = {"off": 0, "low": 85, "medium": 170, "high": 255}


def _word_for_brightness(brightness: int) -> str:
    """Nearest level word for a 0-255 dimmer value."""
    if brightness <= 0:
        return "off"
    best = min(
        (w for w in LEVEL_WORDS if w != "off"),
        key=lambda w: abs(LEVEL_TO_BRIGHTNESS[w] - brightness),
    )
    return "low" if brightness < 60 else best


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SleepNumberConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the under-bed light when the ASCII transport is available."""
    data = entry.runtime_data
    ble = data.ble
    if getattr(ble, "mode", None) != "ascii":
        return
    for bed in data.client.beds.values():
        async_add_entities([UnderBedLight(data.status, entry, ble, bed)])


class UnderBedLight(LightEntity):
    """The bed's shared under-bed fixture, driven with UBLS/UBAG."""

    _attr_has_entity_name = True
    _attr_translation_key = "underbed_light"
    _attr_icon = "mdi:bed-double-outline"
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_features = LightEntityFeature(0)

    def __init__(self, coordinator, entry, ble, bed) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._ble = ble
        self.bed = bed
        self._attr_unique_id = f"{bed.id}_underbed_light"
        self._attr_name = None  # device name gives context

    # -- state ------------------------------------------------------------

    def _snapshot(self) -> AsciiSnapshot | None:
        st = getattr(self.coordinator, "_ascii_snap", None)
        if st is not None:
            return st
        return getattr(self._entry.runtime_data, "last_snapshot", None)

    @callback
    def _handle_coordinator_update(self) -> None:
        snap = self._snapshot()
        if snap is not None and snap.light:
            level = snap.light.split()[0] if snap.light.split() else "off"
            self._attr_color_mode = ColorMode.BRIGHTNESS
            self._attr_brightness = LEVEL_TO_BRIGHTNESS.get(level, 0)
            self._attr_is_on = bool(self._attr_brightness)
            self._attr_extra_state_attributes = {"level_word": level}
        super()._handle_coordinator_update()

    # -- commands ----------------------------------------------------------

    async def async_turn_on(self, **kwargs) -> None:
        brightness = kwargs.get("brightness")
        level = _word_for_brightness(brightness) if brightness else "medium"
        await self._ble.ascii_light(level)
        self._attr_brightness = LEVEL_TO_BRIGHTNESS[level]
        self._attr_is_on = True
        self._attr_extra_state_attributes = {"level_word": level}
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        await self._ble.ascii_light("off")
        self._attr_brightness = 0
        self._attr_is_on = False
        self._attr_extra_state_attributes = {"level_word": "off"}
        self.async_write_ha_state()
