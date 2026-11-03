"""Tests for the Teltonika NTP Server config flow."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.teltonika_ntp.api import (
    TeltonikaAuthError,
    TeltonikaCertificateError,
    TeltonikaConnectionError,
)
from custom_components.teltonika_ntp.const import CONF_FINGERPRINT, DOMAIN
from homeassistant.config_entries import SOURCE_DHCP, SOURCE_USER
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo

from .conftest import (
    BASE_URL,
    DEVICE_STATUS,
    FINGERPRINT,
    HOST,
    MAC,
    NEW_FINGERPRINT,
    PUBLIC_INFO,
    SERIAL,
)

pytestmark = pytest.mark.usefixtures("mock_setup_entry")

USER_INPUT = {
    CONF_HOST: HOST,
    CONF_USERNAME: "homeassistant",
    CONF_PASSWORD: "secret",
    CONF_VERIFY_SSL: False,
}
CREDENTIALS = {CONF_USERNAME: "homeassistant", CONF_PASSWORD: "secret"}
DHCP_INFO = DhcpServiceInfo(ip=HOST, hostname="ntp001", macaddress="209727000001")
PINNED_ENTRY_DATA = {**USER_INPUT, CONF_HOST: BASE_URL, CONF_FINGERPRINT: FINGERPRINT}


async def _start_user_flow(hass: HomeAssistant) -> dict:
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})


async def test_user_flow(
    hass: HomeAssistant, mock_client: MagicMock, mock_fingerprint: AsyncMock
) -> None:
    """A manual setup uses HTTPS, pins the certificate and logs in to that certificate."""
    result = await _start_user_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "NTP001"
    assert result["data"] == PINNED_ENTRY_DATA
    assert result["result"].unique_id == SERIAL

    mock_fingerprint.assert_awaited_once()
    assert mock_fingerprint.await_args.args[0] == BASE_URL
    assert mock_client.flow_class.call_args.kwargs["fingerprint"] == FINGERPRINT
    mock_client.async_logout.assert_awaited()


async def test_user_flow_never_falls_back_to_http(
    hass: HomeAssistant, mock_client: MagicMock
) -> None:
    """When HTTPS does not answer the flow reports an error instead of trying HTTP."""
    mock_client.async_get_public_info.side_effect = TeltonikaConnectionError("no https")
    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
    base_urls = [call.args[1] for call in mock_client.flow_class.call_args_list]
    assert base_urls == [BASE_URL]
    assert mock_client.async_logout.await_count == 1


async def test_user_flow_explicit_http(
    hass: HomeAssistant, mock_client: MagicMock, mock_fingerprint: AsyncMock
) -> None:
    """HTTP is used only when typed, and then nothing is pinned."""
    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_HOST: f"http://{HOST}"}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HOST] == f"http://{HOST}"
    assert CONF_FINGERPRINT not in result["data"]
    mock_fingerprint.assert_not_awaited()


async def test_user_flow_verify_ssl(
    hass: HomeAssistant, mock_client: MagicMock, mock_fingerprint: AsyncMock
) -> None:
    """With CA verification on, the certificate is verified instead of pinned."""
    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_VERIFY_SSL: True}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_VERIFY_SSL] is True
    assert CONF_FINGERPRINT not in result["data"]
    mock_fingerprint.assert_not_awaited()


async def test_user_flow_certificate_unreadable(
    hass: HomeAssistant, mock_client: MagicMock, mock_fingerprint: AsyncMock
) -> None:
    """If the certificate cannot be read, no login is attempted."""
    mock_fingerprint.side_effect = TeltonikaConnectionError("no TLS")
    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
    mock_client.flow_class.assert_not_called()


@pytest.mark.parametrize(
    "host",
    [
        "",
        "   ",
        "exa mple",
        f"{HOST}/admin",
        f"user@{HOST}",
        f"{HOST}?a=1",
        f"{HOST}#x",
        f"ftp://{HOST}",
        f"{HOST}:99999",
        "javascript:alert(1)",
        "a" * 300,
    ],
)
async def test_user_flow_invalid_host(
    hass: HomeAssistant, mock_client: MagicMock, mock_fingerprint: AsyncMock, host: str
) -> None:
    """Addresses with paths, credentials, other schemes or junk are refused up front."""
    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_HOST: host}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_HOST: "invalid_host"}
    mock_fingerprint.assert_not_awaited()
    mock_client.flow_class.assert_not_called()


@pytest.mark.parametrize(
    ("side_effect", "status", "error"),
    [
        (TeltonikaAuthError("denied"), DEVICE_STATUS, "invalid_auth"),
        (TeltonikaConnectionError("down"), DEVICE_STATUS, "cannot_connect"),
        (TeltonikaCertificateError("changed"), DEVICE_STATUS, "cannot_connect"),
        (RuntimeError("boom"), DEVICE_STATUS, "unknown"),
        (None, {"mnfinfo": {}, "static": {}}, "cannot_connect"),
        (None, {"mnfinfo": ["not", "a", "dict"], "static": "junk"}, "cannot_connect"),
    ],
)
async def test_user_flow_errors(
    hass: HomeAssistant,
    mock_client: MagicMock,
    side_effect: Exception | None,
    status: dict,
    error: str,
) -> None:
    """Errors are shown on the form and the flow recovers afterwards."""
    mock_client.async_get_device_status.side_effect = side_effect
    mock_client.async_get_device_status.return_value = status

    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}

    mock_client.async_get_device_status.side_effect = None
    mock_client.async_get_device_status.return_value = DEVICE_STATUS
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_cleans_device_text(hass: HomeAssistant, mock_client: MagicMock) -> None:
    """Names from the device are cleaned and shortened before they become the title."""
    mock_client.async_get_device_status.return_value = {
        **DEVICE_STATUS,
        "static": {"device_name": "NTP\n001\x1b[31m" + "x" * 200},
    }
    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"].startswith("NTP 001")
    assert len(result["title"]) == 64
    assert "\n" not in result["title"] and "\x1b" not in result["title"]


async def test_user_flow_already_configured(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """The same device cannot be added twice, and the existing entry is left alone."""
    mock_config_entry.add_to_hass(hass)

    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_HOST: "192.0.2.99"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert mock_config_entry.data[CONF_HOST] == BASE_URL


async def test_dhcp_flow(
    hass: HomeAssistant, mock_client: MagicMock, mock_fingerprint: AsyncMock
) -> None:
    """A discovered NTP server asks for credentials, then pins and creates an entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_DHCP}, data=DHCP_INFO
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "dhcp_confirm"
    assert result["description_placeholders"] == {"name": "NTP001", "host": HOST}
    # Discovery itself only reads public data: nothing pinned, no login yet.
    mock_fingerprint.assert_not_awaited()
    mock_client.async_get_device_status.assert_not_awaited()

    result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == PINNED_ENTRY_DATA
    assert result["result"].unique_id == SERIAL
    mock_fingerprint.assert_awaited_once()


async def test_dhcp_confirm_error(hass: HomeAssistant, mock_client: MagicMock) -> None:
    """Wrong credentials on a discovered device show an error and can be retried."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_DHCP}, data=DHCP_INFO
    )
    mock_client.async_get_device_status.side_effect = TeltonikaAuthError("denied")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}

    mock_client.async_get_device_status.side_effect = None
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_dhcp_cannot_connect(hass: HomeAssistant, mock_client: MagicMock) -> None:
    """A device that does not answer over HTTPS is not offered."""
    mock_client.async_get_public_info.side_effect = TeltonikaConnectionError("down")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_DHCP}, data=DHCP_INFO
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cannot_connect"


async def test_dhcp_invalid_address(hass: HomeAssistant, mock_client: MagicMock) -> None:
    """A malformed discovery address is ignored."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_DHCP},
        data=DhcpServiceInfo(ip="not an ip", hostname="x", macaddress="209727000001"),
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cannot_connect"


async def test_dhcp_not_supported(hass: HomeAssistant, mock_client: MagicMock) -> None:
    """Teltonika routers are left to the core integration."""
    mock_client.async_get_public_info.return_value = {
        **PUBLIC_INFO,
        "device_name": "RUTX11",
        "device_model": "RUTX11",
    }
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_DHCP}, data=DHCP_INFO
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_supported"


@pytest.mark.parametrize(
    ("stored_host", "expected_host"),
    [
        ("https://192.0.2.20", BASE_URL),
        ("http://192.0.2.20", f"http://{HOST}"),
        ("https://192.0.2.20:8443", f"https://{HOST}:8443"),
        (BASE_URL, BASE_URL),
    ],
)
async def test_dhcp_already_configured(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    stored_host: str,
    expected_host: str,
) -> None:
    """A known device is matched by MAC; a new address keeps the scheme and port."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry, data={**mock_config_entry.data, CONF_HOST: stored_host}
    )
    dr.async_get(hass).async_get_or_create(
        config_entry_id=mock_config_entry.entry_id,
        identifiers={(DOMAIN, SERIAL)},
        connections={(dr.CONNECTION_NETWORK_MAC, MAC)},
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_DHCP}, data=DHCP_INFO
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert mock_config_entry.data[CONF_HOST] == expected_host
    assert mock_config_entry.data[CONF_FINGERPRINT] == FINGERPRINT


async def test_dhcp_ignores_other_domains(hass: HomeAssistant, mock_client: MagicMock) -> None:
    """A device with the same MAC in another integration does not block discovery."""
    other = MockConfigEntry(domain="teltonika", unique_id=SERIAL)
    other.add_to_hass(hass)
    dr.async_get(hass).async_get_or_create(
        config_entry_id=other.entry_id,
        identifiers={("teltonika", SERIAL)},
        connections={(dr.CONNECTION_NETWORK_MAC, MAC)},
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_DHCP}, data=DHCP_INFO
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "dhcp_confirm"


async def test_reauth_flow_keeps_pin(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    mock_fingerprint: AsyncMock,
) -> None:
    """New credentials are sent only to the already pinned certificate."""
    mock_config_entry.add_to_hass(hass)
    result = await mock_config_entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    new_credentials = {CONF_USERNAME: "homeassistant", CONF_PASSWORD: "new-secret"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], new_credentials)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data[CONF_PASSWORD] == "new-secret"
    assert mock_config_entry.data[CONF_FINGERPRINT] == FINGERPRINT
    mock_fingerprint.assert_not_awaited()
    assert mock_client.flow_class.call_args.kwargs["fingerprint"] == FINGERPRINT


async def test_reauth_flow_error(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """A rejected password shows an error and can be retried."""
    mock_config_entry.add_to_hass(hass)
    result = await mock_config_entry.start_reauth_flow(hass)

    mock_client.async_get_device_status.side_effect = TeltonikaAuthError("denied")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}

    mock_client.async_get_device_status.side_effect = None
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"


async def test_reauth_wrong_device(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """Credentials for another device are refused."""
    mock_config_entry.add_to_hass(hass)
    mock_client.async_get_device_status.return_value = {
        **DEVICE_STATUS,
        "mnfinfo": {**DEVICE_STATUS["mnfinfo"], "serial": "9999999999"},
    }
    result = await mock_config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_device"


async def test_reconfigure_flow_repins(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    mock_fingerprint: AsyncMock,
) -> None:
    """Reconfigure changes the address and accepts the certificate the device shows now."""
    mock_config_entry.add_to_hass(hass)
    mock_fingerprint.return_value = NEW_FINGERPRINT
    result = await mock_config_entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    new_input = {**USER_INPUT, CONF_HOST: "192.0.2.30"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], new_input)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert mock_config_entry.data[CONF_HOST] == "https://192.0.2.30"
    assert mock_config_entry.data[CONF_FINGERPRINT] == NEW_FINGERPRINT


async def test_reconfigure_to_verified_certificate(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """Switching to CA verification drops the pin."""
    mock_config_entry.add_to_hass(hass)
    result = await mock_config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_VERIFY_SSL: True}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert mock_config_entry.data[CONF_VERIFY_SSL] is True
    assert CONF_FINGERPRINT not in mock_config_entry.data


async def test_reconfigure_flow_error(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """An unreachable or invalid address shows an error and can be corrected."""
    mock_config_entry.add_to_hass(hass)
    result = await mock_config_entry.start_reconfigure_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_HOST: f"{HOST}/x"}
    )
    assert result["errors"] == {CONF_HOST: "invalid_host"}

    mock_client.async_get_public_info.side_effect = TeltonikaConnectionError("down")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}

    mock_client.async_get_public_info.side_effect = None
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"


async def test_reconfigure_wrong_device(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """Pointing an entry at another device is refused."""
    mock_config_entry.add_to_hass(hass)
    mock_client.async_get_device_status.return_value = {
        **DEVICE_STATUS,
        "mnfinfo": {**DEVICE_STATUS["mnfinfo"], "serial": "9999999999"},
    }
    result = await mock_config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_device"
