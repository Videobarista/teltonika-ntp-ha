"""Data update coordinator for the Teltonika NTP Server integration."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    RutOSClient,
    TeltonikaAuthError,
    TeltonikaCertificateError,
    TeltonikaConnectionError,
    TeltonikaError,
    hostname_from_url,
)
from .const import (
    BACKOFF_KISS_CODES,
    DOMAIN,
    ISSUE_CERTIFICATE_MISMATCH,
    MANUFACTURER,
    MAX_SCAN_INTERVAL,
    SCAN_INTERVAL,
    SYNC_NO_RESPONSE,
    SYNC_RATE_LIMITED,
    SYNC_SYNCHRONIZED,
    SYNC_UNSYNCHRONIZED,
)
from .ntp import NtpError, NtpResult, async_query
from .util import as_dict, clean_text

if TYPE_CHECKING:
    from . import TeltonikaNtpConfigEntry

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class TeltonikaNtpData:
    """Snapshot of everything the entities show."""

    ntp: NtpResult | None
    ntp_error: str | None

    @property
    def sync_state(self) -> str:
        """Return the synchronization state for the enum sensor."""
        if self.ntp is None:
            return SYNC_NO_RESPONSE
        if self.ntp.kiss_code == "RATE":
            return SYNC_RATE_LIMITED
        if self.ntp.is_synchronized:
            return SYNC_SYNCHRONIZED
        return SYNC_UNSYNCHRONIZED


class TeltonikaNtpCoordinator(DataUpdateCoordinator[TeltonikaNtpData]):
    """Poll the device's NTP service and RutOS API."""

    config_entry: TeltonikaNtpConfigEntry
    device_info: DeviceInfo

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: TeltonikaNtpConfigEntry,
        client: RutOSClient,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=SCAN_INTERVAL,
        )
        self.client = client
        self.ntp_host = hostname_from_url(client.base_url)
        self.device_status: dict[str, Any] = {}

    async def _async_setup(self) -> None:
        """Read the device identity once, before the first poll."""
        try:
            self.device_status = await self.client.async_get_device_status()
        except TeltonikaCertificateError as err:
            self._async_create_certificate_issue()
            raise ConfigEntryError(
                "The device presented another TLS certificate than the pinned one"
            ) from err
        except TeltonikaAuthError as err:
            raise ConfigEntryAuthFailed(f"Authentication failed: {err}") from err
        except TeltonikaError as err:
            raise ConfigEntryNotReady(f"Cannot read device status: {err}") from err

        mnf = as_dict(self.device_status.get("mnfinfo"))
        static = as_dict(self.device_status.get("static"))
        serial = (
            clean_text(mnf.get("serial"))
            or self.config_entry.unique_id
            or self.config_entry.entry_id
        )
        macs = {mac for key in ("mac", "macEth") if (mac := _format_mac(mnf.get(key)))}

        self.device_info = DeviceInfo(
            identifiers={(DOMAIN, serial)},
            connections={(CONNECTION_NETWORK_MAC, mac) for mac in macs},
            manufacturer=MANUFACTURER,
            model=clean_text(static.get("model")) or clean_text(static.get("device_name")),
            model_id=clean_text(mnf.get("name")),
            name=clean_text(static.get("device_name")) or self.config_entry.title,
            hw_version=clean_text(mnf.get("hwver")),
            sw_version=clean_text(static.get("fw_version")),
            serial_number=serial,
            configuration_url=self.client.base_url,
        )

    async def _async_update_data(self) -> TeltonikaNtpData:
        """Query the NTP service."""
        ntp: NtpResult | None = None
        ntp_error: str | None = None
        try:
            ntp = await async_query(self.ntp_host)
        except NtpError as err:
            ntp_error = str(err)
            _LOGGER.debug("NTP query to %s failed: %s", self.ntp_host, err)

        self._async_adjust_interval(ntp)

        if ntp is None:
            # Without an NTP reply, check whether the device itself is still up so
            # the entities can tell "service down" apart from "device gone".
            try:
                await self.client.async_get_public_info()
            except TeltonikaCertificateError as err:
                self._async_create_certificate_issue()
                raise UpdateFailed(
                    f"Device {self.ntp_host} presented another TLS certificate"
                ) from err
            except TeltonikaConnectionError as err:
                raise UpdateFailed(f"Device {self.ntp_host} is unreachable: {err}") from err

        return TeltonikaNtpData(ntp=ntp, ntp_error=ntp_error)

    def _async_adjust_interval(self, ntp: NtpResult | None) -> None:
        """Back off while the server refuses us, as RFC 5905 asks of clients."""
        if ntp is not None and ntp.kiss_code in BACKOFF_KISS_CODES:
            current = self.update_interval or SCAN_INTERVAL
            self.update_interval = min(current * 2, MAX_SCAN_INTERVAL)
            _LOGGER.debug(
                "NTP server %s answered %s; next query in %s",
                self.ntp_host,
                ntp.kiss_code,
                self.update_interval,
            )
        elif ntp is not None and self.update_interval != SCAN_INTERVAL:
            self.update_interval = SCAN_INTERVAL

    def _async_create_certificate_issue(self) -> None:
        """Tell the user in Repairs that the certificate changed."""
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"{ISSUE_CERTIFICATE_MISMATCH}_{self.config_entry.entry_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_CERTIFICATE_MISMATCH,
            translation_placeholders={"name": self.config_entry.title},
        )


def _format_mac(mac: Any) -> str | None:
    """Return a MAC as aa:bb:cc:dd:ee:ff, whatever separators RutOS used, or None."""
    if not isinstance(mac, str) or len(mac) > 32:
        return None
    digits = "".join(char for char in mac if char not in ":-. ").lower()
    if len(digits) != 12 or any(char not in "0123456789abcdef" for char in digits):
        return None
    return ":".join(digits[i : i + 2] for i in range(0, 12, 2))
