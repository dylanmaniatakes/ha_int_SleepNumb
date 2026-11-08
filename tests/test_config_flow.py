"""Config-flow tests for Sleep Number Local-First."""

from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.sleepnumber_pro.const import CONF_BLE_ADDRESS, DOMAIN
from custom_components.sleepnumber_pro.sleepiq_local import SleepIQLoginException


@contextlib.contextmanager
def patch_ble_available(avail: bool, address: str):
    """Mock the BLE transport so the flow's local-only validation answers."""
    fake = MagicMock()
    fake.available = AsyncMock(return_value=avail)
    with patch(
        "custom_components.sleepnumber_pro.bluetooth.BleBridgeClient",
        return_value=fake,
    ):
        yield fake

USER_INPUT = {CONF_USERNAME: "user@example.com", CONF_PASSWORD: "secret"}


async def test_user_flow_success(hass: HomeAssistant, patched_client) -> None:
    """A valid login creates the entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "user@example.com"
    assert result["data"] == USER_INPUT


async def test_user_flow_invalid_auth(hass: HomeAssistant, patched_client) -> None:
    """A bad login surfaces an error and stays on the form."""
    patched_client.login = AsyncMock(side_effect=SleepIQLoginException("nope"))
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_single_account_only(hass: HomeAssistant, patched_client, config_entry) -> None:
    """A second setup of the same account aborts."""
    config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# --- Local-only (BLE) setup: no credentials required -------------------------

async def test_user_flow_ble_only(hass: HomeAssistant) -> None:
    """A BLE address alone creates a local-only entry (no cloud login)."""
    ble_addr = "80:16:09:07:C2:7D"
    with (
        patch_ble_available(True, ble_addr),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        assert result["type"] is FlowResultType.FORM
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_BLE_ADDRESS: ble_addr}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == f"Sleep Number Local ({ble_addr})"
    assert result["data"][CONF_BLE_ADDRESS] == ble_addr


async def test_user_flow_ble_unreachable(hass: HomeAssistant) -> None:
    """A BLE address that no adapter can see surfaces cannot_connect."""
    ble_addr = "80:16:09:07:C2:7D"
    with patch_ble_available(False, ble_addr):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_BLE_ADDRESS: ble_addr}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_missing_both(hass: HomeAssistant) -> None:
    """Neither credentials nor a BLE address = explicit form error."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "missing_credentials_or_ble"}
