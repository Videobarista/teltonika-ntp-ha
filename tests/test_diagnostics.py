"""Tests for the diagnostics of the Teltonika NTP Server integration."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.teltonika_ntp.diagnostics import async_get_config_entry_diagnostics
from custom_components.teltonika_ntp.ntp import NtpTimeoutError
from homeassistant.core import HomeAssistant

from .conftest import FINGERPRINT, HOST, SERIAL


async def test_diagnostics_are_redacted(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_ntp: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Credentials, address, certificate, serial number and MACs never leave."""
    mock_ntp.side_effect = NtpTimeoutError(f"No reply from {HOST}:123 within 5 s")
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)

    diagnostics = await async_get_config_entry_diagnostics(hass, mock_config_entry)
    dumped = json.dumps(diagnostics)

    for secret in ("secret", "homeassistant", HOST, FINGERPRINT, SERIAL, "209727000001"):
        assert secret not in dumped
    assert diagnostics["uses_https"] is True
    assert diagnostics["certificate_pinned"] is True
    assert diagnostics["poll_interval_seconds"] == 60
    assert diagnostics["sync_state"] == "no_response"
