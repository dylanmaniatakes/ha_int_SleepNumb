"""Sensors for Sleep Number Local-First.

Two coordinators feed sensors here: the live bed-sensor plane (sleep number,
pressure) and the nightly sleep-health record (score, heart rate, respiration,
HRV, durations).  All are per-sleeper.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant, callback

from ._compat import AddConfigEntryEntitiesCallback
from .const import (
    CONNECTION,
    HEART_RATE,
    HRV,
    PRESSURE,
    RESPIRATORY_RATE,
    RESTFUL,
    RESTLESS,
    SLEEP_DURATION,
    SLEEP_NUMBER,
    SLEEP_SCORE,
    SOURCE_BLE,
    SOURCE_CLOUD,
    SOURCE_LOCAL,
)
from .coordinator import SleepNumberConfigEntry
from .entity import SleepNumberBedEntity, SleepNumberSleeperEntity
from .sleepiq_local import SleepIQSleeper


@dataclass(frozen=True, kw_only=True)
class SleepNumberSensorDescription(SensorEntityDescription):
    """Describes a Sleep Number sensor."""

    value_fn: Callable[[SleepIQSleeper], float | int | None]


# Live plane (status coordinator)
LIVE_SENSORS: tuple[SleepNumberSensorDescription, ...] = (
    SleepNumberSensorDescription(
        key=SLEEP_NUMBER,
        translation_key=SLEEP_NUMBER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: s.sleep_number or None,
    ),
    SleepNumberSensorDescription(
        key=PRESSURE,
        translation_key=PRESSURE,
        state_class=SensorStateClass.MEASUREMENT,
        # Live over the ASCII plane (360-gen controller SNCG), cloud fallback:
        # enabled by default now that a local source exists.
        value_fn=lambda s: s.pressure or None,
    ),
)

# Nightly plane (sleep-data coordinator)
SLEEP_SENSORS: tuple[SleepNumberSensorDescription, ...] = (
    SleepNumberSensorDescription(
        key=SLEEP_SCORE,
        translation_key=SLEEP_SCORE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="score",
        value_fn=lambda s: s.sleep_data.sleep_score if s.sleep_data else None,
    ),
    SleepNumberSensorDescription(
        key=HEART_RATE,
        translation_key=HEART_RATE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="bpm",
        value_fn=lambda s: s.sleep_data.heart_rate if s.sleep_data else None,
    ),
    SleepNumberSensorDescription(
        key=RESPIRATORY_RATE,
        translation_key=RESPIRATORY_RATE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="br/min",
        value_fn=lambda s: s.sleep_data.respiratory_rate if s.sleep_data else None,
    ),
    SleepNumberSensorDescription(
        key=HRV,
        translation_key=HRV,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="ms",
        value_fn=lambda s: s.sleep_data.hrv if s.sleep_data else None,
    ),
    SleepNumberSensorDescription(
        key=SLEEP_DURATION,
        translation_key=SLEEP_DURATION,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTime.HOURS,
        suggested_display_precision=1,
        value_fn=lambda s: (
            round(s.sleep_data.duration / 3600, 1)
            if s.sleep_data and s.sleep_data.duration
            else None
        ),
    ),
    SleepNumberSensorDescription(
        key=RESTFUL,
        translation_key=RESTFUL,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTime.HOURS,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: (
            round(s.sleep_data.restful / 3600, 1)
            if s.sleep_data and s.sleep_data.restful
            else None
        ),
    ),
    SleepNumberSensorDescription(
        key=RESTLESS,
        translation_key=RESTLESS,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTime.HOURS,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: (
            round(s.sleep_data.restless / 3600, 1)
            if s.sleep_data and s.sleep_data.restless
            else None
        ),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SleepNumberConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Sleep Number sensors."""
    data = entry.runtime_data
    entities: list[SensorEntity] = []
    for bed in data.client.beds.values():
        entities.append(ConnectionSensor(data.status, bed))
        for sleeper in bed.sleepers:
            entities.extend(
                SleepNumberSensor(data.status, bed, sleeper, desc)
                for desc in LIVE_SENSORS
            )
            entities.extend(
                SleepNumberSensor(data.sleep, bed, sleeper, desc)
                for desc in SLEEP_SENSORS
            )
    async_add_entities(entities)


class ConnectionSensor(SleepNumberBedEntity, SensorEntity):
    """Which transport is currently serving this bed: local hub or cloud."""

    _attr_translation_key = CONNECTION
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [SOURCE_BLE, SOURCE_LOCAL, SOURCE_CLOUD]

    def __init__(self, coordinator, bed) -> None:
        super().__init__(coordinator, bed)
        self._attr_unique_id = f"{bed.id}_{CONNECTION}"

    @callback
    def _async_update_attrs(self) -> None:
        source = getattr(self.coordinator, "source", SOURCE_CLOUD)
        self._attr_native_value = source
        self._attr_icon = {
            SOURCE_BLE: "mdi:bluetooth",
            SOURCE_LOCAL: "mdi:lan-connect",
        }.get(source, "mdi:cloud-outline")


class SleepNumberSensor(SleepNumberSleeperEntity, SensorEntity):
    """A Sleep Number sensor."""

    entity_description: SleepNumberSensorDescription

    def __init__(self, coordinator, bed, sleeper, description) -> None:
        self.entity_description = description
        super().__init__(coordinator, bed, sleeper, description.key)

    @callback
    def _async_update_attrs(self) -> None:
        self._attr_native_value = self.entity_description.value_fn(self.sleeper)
