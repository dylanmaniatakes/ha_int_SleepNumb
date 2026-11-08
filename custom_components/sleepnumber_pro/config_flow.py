"""Config flow for Sleep Number (SleepIQ) Local-First."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import probatio
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_BLE_ADDRESS, CONF_LOCAL_HOST, CONF_LOCAL_TOKEN, DOMAIN
from .sleepiq_local import (
    AsyncSleepIQ,
    SleepIQConnectionException,
    SleepIQLoginException,
    SleepIQTimeoutException,
)

if TYPE_CHECKING:
    from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
    from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo

_LOGGER = logging.getLogger(__name__)


def _user_schema(defaults: dict[str, Any] | None = None) -> probatio.Schema:
    """Credential form; credentials optional when a BLE address is provided.

    The 360-gen controller is fully local — an entry that carries a BLE address
    and no password runs cloud-free (sleep number, presets, light, foot warmer,
    pressure + pressure-derived presence all work without an account).
    """
    defaults = defaults or {}
    fields = {
        probatio.Optional(CONF_USERNAME, default=defaults.get(CONF_USERNAME)): probatio.Maybe(str),
        probatio.Optional(CONF_PASSWORD): probatio.Maybe(str),
        probatio.Optional(
            CONF_BLE_ADDRESS,
            description={"suggested_value": defaults.get(CONF_BLE_ADDRESS)},
        ): probatio.Maybe(str),
        probatio.Optional(
            CONF_LOCAL_HOST,
            description={"suggested_value": defaults.get(CONF_LOCAL_HOST)},
        ): probatio.Maybe(str),
        probatio.Optional(
            CONF_LOCAL_TOKEN,
            description={"suggested_value": defaults.get(CONF_LOCAL_TOKEN)},
        ): probatio.Maybe(str),
    }
    return probatio.Schema(fields)


def _clean(data: dict[str, Any]) -> dict[str, Any]:
    """Drop empty optional values so blank strings are never stored."""
    return {k: v for k, v in data.items() if v not in (None, "")}


async def _validate(
    hass: HomeAssistant, data: dict[str, Any]
) -> str | None:
    """Return an error key, or None if the credentials work."""
    client = AsyncSleepIQ(client_session=async_get_clientsession(hass))
    try:
        await client.login(data[CONF_USERNAME], data[CONF_PASSWORD])
    except SleepIQLoginException:
        return "invalid_auth"
    except (SleepIQTimeoutException, SleepIQConnectionException):
        return "cannot_connect"
    return None


async def _validate_ble(hass: HomeAssistant, address: str) -> str | None:
    """The BLE-only check: is the bed advertise-connectable right now?"""
    from .bluetooth import BleBridgeClient

    client = BleBridgeClient(hass, address)
    return None if await client.available() else "cannot_connect"


class SleepNumberConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovered: dict[str, str] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect credentials (optional with a BLE address) or BLE-only setup."""
        errors: dict[str, str] = {}
        defaults: dict[str, Any] = {}
        if user_input is not None:
            data = _clean(user_input)
            if ble := self._discovered.get("ble"):
                data.setdefault(CONF_BLE_ADDRESS, ble)
            if host := self._discovered.get("host"):
                data.setdefault(CONF_LOCAL_HOST, host)
            has_creds = bool(data.get(CONF_USERNAME)) and bool(data.get(CONF_PASSWORD))
            if not has_creds and not data.get(CONF_BLE_ADDRESS):
                # Nothing to validate against: demand credentials or a BLE target.
                errors["base"] = "missing_credentials_or_ble"
            elif has_creds:
                await self.async_set_unique_id(data[CONF_USERNAME].lower())
                self._abort_if_unique_id_configured()
                if error := await _validate(self.hass, data):
                    errors["base"] = error
                else:
                    return self.async_create_entry(title=data[CONF_USERNAME], data=data)
            else:
                # BLE-only entry: unique id = the bed address.
                ble_addr = data[CONF_BLE_ADDRESS]
                await self.async_set_unique_id(f"local-{ble_addr.lower()}")
                self._abort_if_unique_id_configured()
                if error := await _validate_ble(self.hass, ble_addr):
                    errors["base"] = error
                else:
                    data.setdefault(
                        CONF_USERNAME, data[CONF_BLE_ADDRESS].replace(":", "").lower()
                    )
                    return self.async_create_entry(
                        title=f"Sleep Number Local ({ble_addr})", data=data
                    )
            if data.get(CONF_BLE_ADDRESS):
                defaults[CONF_BLE_ADDRESS] = data[CONF_BLE_ADDRESS]
        if host := self._discovered.get("host"):
            defaults[CONF_LOCAL_HOST] = host
        if ble := self._discovered.get("ble"):
            defaults.setdefault(CONF_BLE_ADDRESS, ble)
        return self.async_show_form(
            step_id="user",
            data_schema=_user_schema(defaults),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change credentials or add/update the local hub bridge address."""
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            data = _clean({**entry.data, **user_input})
            if error := await _validate(self.hass, data):
                errors["base"] = error
            else:
                return self.async_update_reload_and_abort(entry, data=data)

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_user_schema(dict(entry.data)),
            errors=errors,
        )

    async def async_step_dhcp(
        self, discovery_info: DhcpServiceInfo
    ) -> ConfigFlowResult:
        """Handle a SleepIQ hub found on the network (MAC 64:DB:A0:*)."""
        # If an account is already configured, the hub is already covered; just
        # record its current address for the local transport (Phase 2) and stop.
        if self._async_current_entries():
            return self.async_abort(reason="already_configured")
        # A fresh install still needs cloud credentials, so surface the discovered
        # hub address and hand off to the user step.
        self._discovered = {"host": discovery_info.ip}
        return await self.async_step_user()

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle the bed hub found over Bluetooth LE.

        BLE is a no-root local path: the hub's own radio, reachable within range.
        We record its address for the BLE transport; the account still sets up
        with cloud credentials (and can go local via bridge or BLE afterwards).
        """
        if entries := self._async_current_entries():
            entry = entries[0]
            if entry.data.get(CONF_BLE_ADDRESS) != discovery_info.address:
                self.hass.config_entries.async_update_entry(
                    entry,
                    data={**entry.data, CONF_BLE_ADDRESS: discovery_info.address},
                )
            return self.async_abort(reason="already_configured")
        self._discovered = {"ble": discovery_info.address}
        return await self.async_step_user()

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start reauth after an auth failure."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm reauth by re-entering the password."""
        errors: dict[str, str] = {}
        reauth_entry = self._get_reauth_entry()
        if user_input is not None:
            data = {
                CONF_USERNAME: reauth_entry.data[CONF_USERNAME],
                CONF_PASSWORD: user_input[CONF_PASSWORD],
            }
            if error := await _validate(self.hass, data):
                errors["base"] = error
            else:
                return self.async_update_reload_and_abort(reauth_entry, data=data)

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=probatio.Schema({probatio.Required(CONF_PASSWORD): str}),
            errors=errors,
            description_placeholders={CONF_USERNAME: reauth_entry.data[CONF_USERNAME]},
        )
