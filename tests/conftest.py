"""Fixtures for the Teltonika NTP Server tests."""

from __future__ import annotations

from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.teltonika_ntp.const import CONF_FINGERPRINT, DOMAIN
from custom_components.teltonika_ntp.ntp import NtpResult
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME, CONF_VERIFY_SSL

# Documentation addresses (RFC 5737) and made-up identifiers only.
HOST = "192.0.2.10"
BASE_URL = f"https://{HOST}"
SERIAL = "1234567890"
MAC = "20:97:27:00:00:01"
FINGERPRINT = "ab" * 32
NEW_FINGERPRINT = "cd" * 32

PUBLIC_INFO: dict[str, Any] = {
    "lang": "en",
    "device_name": "NTP001",
    "device_model": "NTP001",
    "api_version": "1.9",
}

DEVICE_STATUS: dict[str, Any] = {
    "mnfinfo": {
        "serial": SERIAL,
        "mac": "209727000001",
        "macEth": "209727000002",
        "name": "NTP00100XXXX",
        "hwver": "0004",
    },
    "static": {
        "device_name": "NTP001",
        "model": "NTP001",
        "fw_version": "NTP001_R_00.01.02",
    },
}

ENTRY_DATA: dict[str, Any] = {
    CONF_HOST: BASE_URL,
    CONF_USERNAME: "homeassistant",
    CONF_PASSWORD: "secret",
    CONF_VERIFY_SSL: False,
    CONF_FINGERPRINT: FINGERPRINT,
}

NTP_SYNCHRONIZED = NtpResult(
    leap=0,
    version=4,
    stratum=1,
    poll=4,
    precision=-17,
    root_delay=0.0,
    root_dispersion=0.0013,
    reference_id="GPS",
    kiss_code=None,
    offset=0.0002,
    delay=0.004,
    reference_time=1_791_000_000.0,
    server_time=1_791_000_010.0,
)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Let Home Assistant load the integration from custom_components."""


def _make_client() -> MagicMock:
    """Return a mocked RutOSClient instance that behaves like a healthy NTP001."""
    client = MagicMock()
    client.base_url = BASE_URL
    client.async_get_public_info = AsyncMock(return_value=PUBLIC_INFO)
    client.async_get_device_status = AsyncMock(return_value=DEVICE_STATUS)
    client.async_logout = AsyncMock()
    return client


@pytest.fixture
def mock_client() -> Generator[MagicMock]:
    """Patch the RutOS client everywhere it is created; return the shared instance.

    ``client.flow_class`` and ``client.setup_class`` are the patched classes, so tests
    can check how the client was created (address, TLS settings, pinned certificate).
    """
    client = _make_client()
    with (
        patch(
            "custom_components.teltonika_ntp.config_flow.RutOSClient", return_value=client
        ) as flow_class,
        patch("custom_components.teltonika_ntp.RutOSClient", return_value=client) as setup_class,
    ):
        client.flow_class = flow_class
        client.setup_class = setup_class
        yield client


@pytest.fixture(autouse=True)
def mock_fingerprint() -> Generator[AsyncMock]:
    """Patch reading the device certificate; no test opens a real TLS connection."""
    fetch = AsyncMock(return_value=FINGERPRINT)
    with (
        patch(
            "custom_components.teltonika_ntp.config_flow.async_fetch_certificate_fingerprint",
            new=fetch,
        ),
        patch("custom_components.teltonika_ntp.async_fetch_certificate_fingerprint", new=fetch),
    ):
        yield fetch


@pytest.fixture
def mock_ntp() -> Generator[AsyncMock]:
    """Patch the NTP query with a synchronized stratum 1 reply."""
    with patch(
        "custom_components.teltonika_ntp.coordinator.async_query",
        new=AsyncMock(return_value=NTP_SYNCHRONIZED),
    ) as query:
        yield query


@pytest.fixture
def mock_setup_entry() -> Generator[AsyncMock]:
    """Skip setting up the entry, for config flow tests."""
    with patch(
        "custom_components.teltonika_ntp.async_setup_entry", return_value=True
    ) as setup_entry:
        yield setup_entry


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Return a config entry for the device."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="NTP001",
        unique_id=SERIAL,
        data=dict(ENTRY_DATA),
    )
