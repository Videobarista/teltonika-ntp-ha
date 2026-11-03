"""Diagnostics support for the Teltonika NTP Server integration."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from . import TeltonikaNtpConfigEntry
from .const import CONF_FINGERPRINT

# Credentials, identifiers and network details are left out so a diagnostics file can
# be shared in a public issue.
TO_REDACT = {
    CONF_FINGERPRINT,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_USERNAME,
    "batch",
    "hostname",
    "mac",
    "macEth",
    "serial",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: TeltonikaNtpConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    data = coordinator.data
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "device_status": async_redact_data(coordinator.device_status, TO_REDACT),
        "uses_https": entry.data.get(CONF_HOST, "").startswith("https://"),
        "certificate_pinned": bool(entry.data.get(CONF_FINGERPRINT)),
        "poll_interval_seconds": (
            coordinator.update_interval.total_seconds() if coordinator.update_interval else None
        ),
        "sync_state": data.sync_state if data else None,
        "ntp": asdict(data.ntp) if data and data.ntp else None,
        "ntp_error": (
            data.ntp_error.replace(coordinator.ntp_host, "**REDACTED**")
            if data and data.ntp_error
            else None
        ),
    }
