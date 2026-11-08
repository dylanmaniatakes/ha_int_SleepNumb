"""Switches for Sleep Number Local-First: Responsive Air and privacy pause."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback

from ._compat import AddConfigEntryEntitiesCallback
from .const import PRIVACY, RESPONSIVE_AIR
from .coordinator import SleepNumberConfigEntry
from .entity import SleepNumberBedEntity, SleepNumberSleeperEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SleepNumberConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up switches."""
    data = entry.runtime_data
    # Privacy pause / Responsive Air are cloud-plane commands (set_pause_mode,
    # fetch_responsive_air): a local-only entry has no cloud session, so those
    # entities would never work — don't register them.
    if not data.capabilities.get("creds", True):
        return
    entities: list[SwitchEntity] = []
    for bed in data.client.beds.values():
        entities.append(PrivacySwitch(data.settings, bed))
        if data.capabilities.get("responsive_air"):
            entities.extend(
                ResponsiveAirSwitch(data.settings, bed, sleeper) for sleeper in bed.sleepers
            )
    async_add_entities(entities)


class PrivacySwitch(SleepNumberBedEntity, SwitchEntity):
    """SleepIQ privacy pause (stops the bed reporting to the cloud)."""

    _attr_translation_key = PRIVACY
    _attr_icon = "mdi:shield-account"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, bed) -> None:
        super().__init__(coordinator, bed)
        self._attr_unique_id = f"{bed.id}_{PRIVACY}"

    @callback
    def _async_update_attrs(self) -> None:
        self._attr_is_on = self.bed.paused

    async def async_turn_on(self, **kwargs) -> None:
        await self.bed.set_pause_mode(True)
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        await self.bed.set_pause_mode(False)
        self._attr_is_on = False
        self.async_write_ha_state()


class ResponsiveAirSwitch(SleepNumberSleeperEntity, SwitchEntity):
    """Responsive Air automatic firmness adjustment, per sleeper."""

    _attr_icon = "mdi:air-filter"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, bed, sleeper) -> None:
        super().__init__(coordinator, bed, sleeper, RESPONSIVE_AIR)

    @callback
    def _async_update_attrs(self) -> None:
        self._attr_is_on = self.bed.responsive_air_enabled(self.sleeper.side)

    async def async_turn_on(self, **kwargs) -> None:
        await self.bed.set_responsive_air(self.sleeper.side, True)
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        await self.bed.set_responsive_air(self.sleeper.side, False)
        self._attr_is_on = False
        self.async_write_ha_state()
