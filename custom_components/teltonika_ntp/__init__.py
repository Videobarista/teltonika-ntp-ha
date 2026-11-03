"""The Teltonika NTP Server integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util.ssl import client_context_no_verify

from .api import (
    RutOSClient,
    TeltonikaConnectionError,
    async_fetch_certificate_fingerprint,
    is_https,
    valid_fingerprint,
)
from .const import CONF_FINGERPRINT, DOMAIN, ISSUE_CERTIFICATE_MISMATCH, ISSUE_INSECURE_HTTP
from .coordinator import TeltonikaNtpCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR]

type TeltonikaNtpConfigEntry = ConfigEntry[TeltonikaNtpCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: TeltonikaNtpConfigEntry) -> bool:
    """Set up a Teltonika NTP server from a config entry."""
    base_url: str = entry.data[CONF_HOST]
    verify_ssl: bool = entry.data.get(CONF_VERIFY_SSL, False)
    fingerprint = valid_fingerprint(entry.data.get(CONF_FINGERPRINT))

    if is_https(base_url) and not verify_ssl and fingerprint is None:
        # Entries created before certificate pinning existed: pin the certificate the
        # device presents now (trust on first use), before any credentials are sent.
        try:
            fingerprint = await async_fetch_certificate_fingerprint(
                base_url, client_context_no_verify()
            )
        except TeltonikaConnectionError as err:
            raise ConfigEntryNotReady(f"Cannot read the device certificate: {err}") from err
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_FINGERPRINT: fingerprint}
        )
        _LOGGER.info("Pinned the TLS certificate of %s", entry.title)

    _async_update_http_issue(hass, entry, base_url)

    client = RutOSClient(
        async_get_clientsession(hass),
        base_url,
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
        verify_ssl=verify_ssl,
        fingerprint=fingerprint,
    )
    coordinator = TeltonikaNtpCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    ir.async_delete_issue(hass, DOMAIN, f"{ISSUE_CERTIFICATE_MISMATCH}_{entry.entry_id}")

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: TeltonikaNtpConfigEntry) -> bool:
    """Unload a config entry and close the API session on the device."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.client.async_logout()
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: TeltonikaNtpConfigEntry) -> None:
    """Remove the repair issues of a deleted entry."""
    for issue in (ISSUE_CERTIFICATE_MISMATCH, ISSUE_INSECURE_HTTP):
        ir.async_delete_issue(hass, DOMAIN, f"{issue}_{entry.entry_id}")


def _async_update_http_issue(
    hass: HomeAssistant, entry: TeltonikaNtpConfigEntry, base_url: str
) -> None:
    """Warn in Repairs while the entry sends its credentials over plain HTTP."""
    issue_id = f"{ISSUE_INSECURE_HTTP}_{entry.entry_id}"
    if is_https(base_url):
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_INSECURE_HTTP,
        translation_placeholders={"name": entry.title},
    )
