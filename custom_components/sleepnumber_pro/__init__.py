"""The Sleep Number (SleepIQ) Local-First integration."""

from __future__ import annotations

import logging

from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .bluetooth import BleBridgeClient
from .const import (
    CONF_BLE_ADDRESS,
    CONF_LOCAL_HOST,
    CONF_LOCAL_PORT,
    CONF_LOCAL_TOKEN,
)
from .coordinator import (
    SleepNumberConfigEntry,
    SleepNumberData,
    SleepNumberSettingsCoordinator,
    SleepNumberSleepDataCoordinator,
    SleepNumberStatusCoordinator,
)
from .entity import bed_device_info
from .local import DEFAULT_PORT, LocalBridgeClient
from .sleepiq_local import (
    AsyncSleepIQ,
    SleepIQAPIException,
    SleepIQConnectionException,
    SleepIQLoginException,
    SleepIQTimeoutException,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.LIGHT,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]


async def async_setup_entry(hass: HomeAssistant, entry: SleepNumberConfigEntry) -> bool:
    """Set up Sleep Number Local-First from a config entry."""
    session = async_create_clientsession(hass)
    client = AsyncSleepIQ(client_session=session)

    try:
        await client.login(entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD])
    except SleepIQLoginException as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except SleepIQTimeoutException as err:
        raise ConfigEntryNotReady(str(err) or "Timed out during authentication") from err
    except SleepIQConnectionException as err:
        raise ConfigEntryNotReady(str(err) or "Cannot reach SleepIQ") from err

    try:
        await client.init_beds()
    except SleepIQTimeoutException as err:
        raise ConfigEntryNotReady(str(err) or "Timed out during initialization") from err
    except SleepIQConnectionException as err:
        raise ConfigEntryNotReady(str(err) or "Cannot reach SleepIQ") from err
    except SleepIQAPIException as err:
        raise ConfigEntryNotReady(str(err) or "Error reading from SleepIQ") from err

    # Discover which subsystems are actually reachable right now, so platforms
    # only create entities for capabilities the cloud still serves.
    capabilities = {
        "foundation": any(bed.foundation.type for bed in client.beds.values()),
        "responsive_air": False,
        "local": False,
        "ble": False,
    }
    for bed in client.beds.values():
        try:
            await bed.fetch_responsive_air()
            if bed.responsive_air:
                capabilities["responsive_air"] = True
        except SleepIQAPIException as err:
            _LOGGER.debug("Responsive Air not available for %s: %s", bed.name, err)

    # Local transports, preferred over cloud when reachable. Order of preference:
    # BLE (no root, in-range) -> on-hub bridge (rooted) -> cloud fallback.
    local: LocalBridgeClient | BleBridgeClient | None = None
    ble: BleBridgeClient | None = None

    if ble_addr := entry.data.get(CONF_BLE_ADDRESS):
        ble = BleBridgeClient(hass, ble_addr)
        capabilities["ble"] = await ble.available()
        local = ble  # BLE is the preferred live transport
        if not capabilities["ble"]:
            _LOGGER.warning(
                "Bed %s not reachable over BLE yet (need an in-range adapter or "
                "proxy); using cloud until it is", ble_addr
            )

    if local is None and (
        host := (entry.data.get(CONF_LOCAL_HOST) or entry.options.get(CONF_LOCAL_HOST))
    ):
        local = LocalBridgeClient(
            session,
            host,
            int(entry.data.get(CONF_LOCAL_PORT, DEFAULT_PORT)),
            entry.data.get(CONF_LOCAL_TOKEN),
        )
        capabilities["local"] = await local.available()
        if not capabilities["local"]:
            _LOGGER.warning(
                "Local bridge at %s did not answer; using cloud until it does", host
            )

    status = SleepNumberStatusCoordinator(hass, entry, client, local=local)
    settings = SleepNumberSettingsCoordinator(hass, entry, client, ble=ble)
    sleep = SleepNumberSleepDataCoordinator(hass, entry, client)

    await status.async_config_entry_first_refresh()
    await settings.async_config_entry_first_refresh()
    await sleep.async_config_entry_first_refresh()

    entry.runtime_data = SleepNumberData(
        client=client,
        status=status,
        settings=settings,
        sleep=sleep,
        capabilities=capabilities,
        ble=ble,
    )
    # Mirror the ASCII-plane detection into capabilities for the platforms
    # (light/foot warm only register when the 360-gen controller answered).
    if ble is not None and ble.mode == "ascii":
        capabilities["ascii"] = True
        _LOGGER.info(
            "360-gen controller answered on the ASCII bamkey plane — "
            "under-bed light, foot warmer and live pressure entities enabled"
        )

    # Register the bed hub devices before the platforms run, so sleeper
    # entities can link via_device_id to an already-registered parent.
    device_registry = dr.async_get(hass)
    for bed in client.beds.values():
        device_registry.async_get_or_create(
            config_entry_id=entry.entry_id, **bed_device_info(bed)
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SleepNumberConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
