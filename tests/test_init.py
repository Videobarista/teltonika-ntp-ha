"""Tests for setting up and unloading the Teltonika NTP Server integration."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.teltonika_ntp.api import (
    TeltonikaAuthError,
    TeltonikaCertificateError,
    TeltonikaConnectionError,
)
from custom_components.teltonika_ntp.const import (
    CONF_FINGERPRINT,
    DOMAIN,
    ISSUE_CERTIFICATE_MISMATCH,
    ISSUE_INSECURE_HTTP,
    MAX_SCAN_INTERVAL,
    SCAN_INTERVAL,
)
from custom_components.teltonika_ntp.ntp import NtpResult, NtpTimeoutError
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import CONF_HOST, STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, issue_registry as ir

from .conftest import DEVICE_STATUS, FINGERPRINT, HOST, NTP_SYNCHRONIZED, SERIAL

RATE_REPLY = NtpResult(0, 4, 0, 3, 0, 0.0, 0.0, "RATE", "RATE", None, None)


def _issue(hass: HomeAssistant, kind: str, entry: MockConfigEntry) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, f"{kind}_{entry.entry_id}")


async def test_setup_and_unload(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_ntp: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """The entry loads, creates the device and entities, and unloads cleanly."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.LOADED

    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, SERIAL), mock_config_entry.entry_id
    )
    assert device is not None
    assert device.model == "NTP001"
    assert device.sw_version == "NTP001_R_00.01.02"
    assert (dr.CONNECTION_NETWORK_MAC, "20:97:27:00:00:01") in device.connections

    assert hass.states.get("sensor.ntp001_synchronization").state == "synchronized"
    assert hass.states.get("sensor.ntp001_stratum").state == "1"
    assert hass.states.get("sensor.ntp001_reference").state == "GPS"
    assert float(hass.states.get("sensor.ntp001_offset").state) == pytest.approx(0.2)
    assert hass.states.get("sensor.ntp001_last_gps_sync").state == (
        datetime.fromtimestamp(1_791_000_000, tz=UTC).isoformat()
    )
    # 1.3 ms root dispersion plus 10 s since the last update at 15 us/s.
    assert float(hass.states.get("sensor.ntp001_maximum_error").state) == pytest.approx(1.45)
    assert hass.states.get("binary_sensor.ntp001_ntp_service").state == STATE_ON
    assert hass.states.get("binary_sensor.ntp001_time_sync").state == STATE_OFF

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.NOT_LOADED
    mock_client.async_logout.assert_awaited()


async def test_setup_auth_failed(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_ntp: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Rejected credentials stop setup and start a reauthentication flow."""
    mock_client.async_get_device_status.side_effect = TeltonikaAuthError("denied")
    mock_config_entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR

    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == [SOURCE_REAUTH]


async def test_setup_retry_when_api_unreachable(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_ntp: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """An unreachable API postpones setup."""
    mock_client.async_get_device_status.side_effect = TeltonikaConnectionError("down")
    mock_config_entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_retry_when_device_offline(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_ntp: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """No NTP reply and no API reply means the device is gone: retry later."""
    mock_ntp.side_effect = NtpTimeoutError("no reply")
    mock_client.async_get_public_info.side_effect = TeltonikaConnectionError("down")
    mock_config_entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


@pytest.mark.parametrize(
    ("ntp_reply", "ntp_error", "sync_state", "service_state"),
    [
        (None, NtpTimeoutError("no reply"), "no_response", STATE_OFF),
        (
            NtpResult(0, 4, 0, 3, 0, 0.0, 0.0, "RATE", "RATE", None, None),
            None,
            "rate_limited",
            STATE_ON,
        ),
        (
            NtpResult(3, 4, 16, 4, -17, 0.0, 0.0, "INIT", None, 0.0, 0.004),
            None,
            "unsynchronized",
            STATE_ON,
        ),
    ],
)
async def test_sync_states(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_ntp: AsyncMock,
    mock_config_entry: MockConfigEntry,
    ntp_reply: NtpResult | None,
    ntp_error: Exception | None,
    sync_state: str,
    service_state: str,
) -> None:
    """Every way the NTP service can answer maps to the right states."""
    mock_ntp.return_value = ntp_reply
    mock_ntp.side_effect = ntp_error
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get("sensor.ntp001_synchronization").state == sync_state
    assert hass.states.get("binary_sensor.ntp001_ntp_service").state == service_state
    assert hass.states.get("binary_sensor.ntp001_time_sync").state == STATE_ON
    assert hass.states.get("sensor.ntp001_last_gps_sync").state == "unknown"
    assert hass.states.get("sensor.ntp001_maximum_error").state == "unknown"


async def test_setup_uses_pinned_certificate(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_ntp: AsyncMock,
    mock_fingerprint: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A pinned entry passes its fingerprint to the client and reads nothing new."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)

    mock_fingerprint.assert_not_awaited()
    assert mock_client.setup_class.call_args.kwargs["fingerprint"] == FINGERPRINT
    assert _issue(hass, ISSUE_INSECURE_HTTP, mock_config_entry) is None


async def test_setup_pins_legacy_entry(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_ntp: AsyncMock,
    mock_fingerprint: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Entries from before certificate pinning get pinned before any login."""
    data = dict(mock_config_entry.data)
    data.pop(CONF_FINGERPRINT)
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(mock_config_entry, data=data)

    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    mock_fingerprint.assert_awaited_once()
    assert mock_config_entry.data[CONF_FINGERPRINT] == FINGERPRINT
    assert mock_client.setup_class.call_args.kwargs["fingerprint"] == FINGERPRINT


async def test_setup_retry_when_certificate_unreadable(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_ntp: AsyncMock,
    mock_fingerprint: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A legacy entry whose certificate cannot be read is retried, never used unpinned."""
    data = dict(mock_config_entry.data)
    data.pop(CONF_FINGERPRINT)
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(mock_config_entry, data=data)
    mock_fingerprint.side_effect = TeltonikaConnectionError("no TLS")

    assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY
    mock_client.setup_class.assert_not_called()


async def test_http_entry_raises_repair_issue(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_ntp: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """An entry on plain HTTP works, warns in Repairs, and the warning goes with it."""
    data = {**mock_config_entry.data, CONF_HOST: f"http://{HOST}"}
    data.pop(CONF_FINGERPRINT)
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(mock_config_entry, data=data)

    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    issue = _issue(hass, ISSUE_INSECURE_HTTP, mock_config_entry)
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.WARNING

    assert await hass.config_entries.async_remove(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert _issue(hass, ISSUE_INSECURE_HTTP, mock_config_entry) is None


async def test_certificate_mismatch_at_setup(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_ntp: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Another certificate stops setup with a Repairs issue; a good setup clears it."""
    mock_client.async_get_device_status.side_effect = TeltonikaCertificateError("changed")
    mock_config_entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    issue = _issue(hass, ISSUE_CERTIFICATE_MISMATCH, mock_config_entry)
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.ERROR

    mock_client.async_get_device_status.side_effect = None
    assert await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert _issue(hass, ISSUE_CERTIFICATE_MISMATCH, mock_config_entry) is None


async def test_certificate_mismatch_while_running(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_ntp: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A changed certificate seen during polling also raises the Repairs issue."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    coordinator = mock_config_entry.runtime_data

    mock_ntp.side_effect = NtpTimeoutError("no reply")
    mock_client.async_get_public_info.side_effect = TeltonikaCertificateError("changed")
    await coordinator.async_refresh()

    assert not coordinator.last_update_success
    assert _issue(hass, ISSUE_CERTIFICATE_MISMATCH, mock_config_entry) is not None
    assert hass.states.get("sensor.ntp001_synchronization").state == "unavailable"


async def test_backoff_on_kiss_of_death(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_ntp: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """RATE doubles the poll interval up to 15 minutes; a normal reply resets it."""
    mock_ntp.return_value = RATE_REPLY
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    coordinator = mock_config_entry.runtime_data
    assert coordinator.update_interval == SCAN_INTERVAL * 2

    for _ in range(6):
        await coordinator.async_refresh()
    assert coordinator.update_interval == MAX_SCAN_INTERVAL

    mock_ntp.return_value = NTP_SYNCHRONIZED
    await coordinator.async_refresh()
    assert coordinator.update_interval == SCAN_INTERVAL


async def test_no_reply_keeps_interval(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_ntp: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Without any reply the interval is left as it is."""
    mock_ntp.side_effect = NtpTimeoutError("no reply")
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    assert mock_config_entry.runtime_data.update_interval == SCAN_INTERVAL


async def test_untrusted_device_data(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_ntp: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Odd types, junk MAC addresses and very long text do not break setup."""
    mock_client.async_get_device_status.return_value = {
        "mnfinfo": {"serial": SERIAL, "mac": "not-a-mac", "macEth": ["x"], "hwver": 4},
        "static": {"device_name": "NTP001", "model": "M" * 500, "fw_version": {"x": 1}},
    }
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)

    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, SERIAL), mock_config_entry.entry_id
    )
    assert device is not None
    assert device.connections == set()
    assert len(device.model) == 64
    assert device.hw_version == "4"
    assert device.sw_version is None


async def test_bad_types_in_device_status(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_ntp: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Sections that are not objects fall back to the entry's own identity."""
    mock_client.async_get_device_status.return_value = {**DEVICE_STATUS, "mnfinfo": "junk"}
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    assert dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, SERIAL), mock_config_entry.entry_id
    )
