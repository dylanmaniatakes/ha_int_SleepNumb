"""Number entities for Sleep Number Local-First (firmness / sleep number)."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.core import HomeAssistant, callback

from ._compat import AddConfigEntryEntitiesCallback
from .const import SLEEP_NUMBER
from .coordinator import SleepNumberConfigEntry
from .entity import SleepNumberSleeperEntity
from .sleepiq_local.consts import Side


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SleepNumberConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up sleep-number controls."""
    data = entry.runtime_data
    async_add_entities(
        SleepNumberNumber(data.status, bed, sleeper, data.ble)
        for bed in data.client.beds.values()
        for sleeper in bed.sleepers
    )


class SleepNumberNumber(SleepNumberSleeperEntity, NumberEntity):
    """Set the firmness (sleep number) for a sleeper."""

    _attr_native_min_value = 5
    _attr_native_max_value = 100
    _attr_native_step = 5
    _attr_mode = NumberMode.SLIDER
    _attr_icon = "mdi:bed-king"

    def __init__(self, coordinator, bed, sleeper, ble=None) -> None:
        super().__init__(coordinator, bed, sleeper, SLEEP_NUMBER)
        self._ble = ble

    @callback
    def _async_update_attrs(self) -> None:
        self._attr_native_value = self.sleeper.sleep_number or None

    async def async_set_native_value(self, value: float) -> None:
        if getattr(self._ble, "mode", None) == "ascii":
            # 360-gen controller: PSNS + settle-poll; the SNCG pressure/state
            # stream updates the slider value live while the pump runs.
            side_word = "left" if self.sleeper.side == Side.LEFT else "right"
            await self._ble.ascii_set_sleep_number(side_word, int(value))
        else:
            await self.sleeper.set_sleepnumber(int(value))
        self._attr_native_value = int(value)
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()
