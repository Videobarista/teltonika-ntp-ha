"""Config flow for the Teltonika NTP Server integration.

The flow structure (user, DHCP discovery, reauthentication) is based on the config flow of
the Home Assistant core Teltonika integration (Apache License 2.0).
"""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import voluptuous as vol
from yarl import URL

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
from homeassistant.util.ssl import client_context_no_verify

from .api import (
    InvalidHostError,
    RutOSClient,
    TeltonikaAuthError,
    TeltonikaError,
    async_fetch_certificate_fingerprint,
    is_https,
    parse_host,
    valid_fingerprint,
)
from .const import CONF_FINGERPRINT, DOMAIN, SUPPORTED_MODEL_PREFIXES
from .util import as_dict, clean_text

_LOGGER = logging.getLogger(__name__)

_PASSWORD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_USERNAME, default="admin"): str,
        vol.Required(CONF_PASSWORD): _PASSWORD,
        vol.Optional(CONF_VERIFY_SSL, default=False): bool,
    }
)
CREDENTIALS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME, default="admin"): str,
        vol.Required(CONF_PASSWORD): _PASSWORD,
    }
)


class CannotConnect(Exception):
    """The device could not be reached."""


class InvalidAuth(Exception):
    """The credentials were rejected."""


class InvalidHost(Exception):
    """The address is not a valid host, IP address or base URL."""


def _entry_for_mac(hass: HomeAssistant, mac: str) -> ConfigEntry | None:
    """Return the entry of this integration whose device has the given MAC address.

    Walks the devices per config entry instead of looking the MAC up globally, which
    works the same on every supported Home Assistant release.
    """
    registry = dr.async_get(hass)
    connection = (dr.CONNECTION_NETWORK_MAC, mac)
    for entry in hass.config_entries.async_entries(DOMAIN):
        for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
            if connection in device.connections:
                return entry
    return None


def _is_supported(model: Any) -> bool:
    """Return True for models this integration is written for."""
    text = clean_text(model)
    return text is not None and text.upper().startswith(SUPPORTED_MODEL_PREFIXES)


def _entry_data(info: Mapping[str, Any], data: Mapping[str, Any]) -> dict[str, Any]:
    """Build config entry data from validated input; the pin is stored only when used."""
    entry_data = {
        CONF_HOST: info["host"],
        CONF_USERNAME: data[CONF_USERNAME],
        CONF_PASSWORD: data[CONF_PASSWORD],
        CONF_VERIFY_SSL: data.get(CONF_VERIFY_SSL, False),
    }
    if info["fingerprint"]:
        entry_data[CONF_FINGERPRINT] = info["fingerprint"]
    return entry_data


async def _async_validate(
    hass: HomeAssistant, data: Mapping[str, Any], pinned: str | None = None
) -> dict[str, Any]:
    """Check the address, pin the certificate, log in and read the device identity.

    Without a scheme only HTTPS is tried: the password never falls back to plain HTTP.
    For HTTPS without CA verification the certificate is pinned first (``pinned`` keeps
    an existing pin, as in reauthentication), so the login goes to that certificate.
    """
    try:
        base_url = parse_host(data[CONF_HOST])
    except InvalidHostError as err:
        raise InvalidHost from err

    verify_ssl = data.get(CONF_VERIFY_SSL, False)
    fingerprint: str | None = None
    if is_https(base_url) and not verify_ssl:
        fingerprint = valid_fingerprint(pinned)
        if fingerprint is None:
            try:
                fingerprint = await async_fetch_certificate_fingerprint(
                    base_url, client_context_no_verify()
                )
            except TeltonikaError as err:
                raise CannotConnect from err

    client = RutOSClient(
        async_get_clientsession(hass),
        base_url,
        data[CONF_USERNAME],
        data[CONF_PASSWORD],
        verify_ssl=verify_ssl,
        fingerprint=fingerprint,
    )
    try:
        await client.async_get_public_info()
        status = await client.async_get_device_status()
    except TeltonikaAuthError as err:
        raise InvalidAuth from err
    except TeltonikaError as err:
        _LOGGER.debug("Cannot use %s: %s", base_url, err)
        raise CannotConnect from err
    finally:
        await client.async_logout()

    mnf = as_dict(status.get("mnfinfo"))
    static = as_dict(status.get("static"))
    serial = clean_text(mnf.get("serial"))
    if not serial:
        raise CannotConnect("Device did not report a serial number")
    return {
        "host": base_url,
        "fingerprint": fingerprint,
        "serial": serial,
        "title": clean_text(static.get("device_name"))
        or clean_text(static.get("model"))
        or "Teltonika NTP",
    }


class TeltonikaNtpConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for a Teltonika NTP server."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._discovered_host: str | None = None

    async def _async_try(
        self, data: Mapping[str, Any], errors: dict[str, str], pinned: str | None = None
    ) -> dict[str, Any] | None:
        """Validate input and fill ``errors``; return device info on success."""
        try:
            return await _async_validate(self.hass, data, pinned)
        except InvalidHost:
            errors[CONF_HOST] = "invalid_host"
        except CannotConnect:
            errors["base"] = "cannot_connect"
        except InvalidAuth:
            errors["base"] = "invalid_auth"
        except Exception:
            _LOGGER.exception("Unexpected error while validating the device")
            errors["base"] = "unknown"
        return None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle manual setup."""
        errors: dict[str, str] = {}
        if user_input is not None:
            info = await self._async_try(user_input, errors)
            if info is not None:
                await self.async_set_unique_id(info["serial"])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=info["title"], data=_entry_data(info, user_input)
                )

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(USER_SCHEMA, user_input),
            errors=errors,
        )

    async def async_step_dhcp(self, discovery_info: DhcpServiceInfo) -> ConfigFlowResult:
        """Handle a Teltonika device found through DHCP."""
        try:
            base_url = parse_host(discovery_info.ip)
        except InvalidHostError:
            return self.async_abort(reason="cannot_connect")

        # Read only the public status here: no credentials are sent before the user
        # confirms, and that confirmation pins the certificate.
        try:
            public = await RutOSClient(
                async_get_clientsession(self.hass), base_url, "", ""
            ).async_get_public_info()
        except TeltonikaError as err:
            _LOGGER.debug("Discovered device at %s did not answer: %s", base_url, err)
            return self.async_abort(reason="cannot_connect")

        # The core "teltonika" integration handles routers; only take NTP servers.
        if not _is_supported(public.get("device_model") or public.get("device_name")):
            return self.async_abort(reason="not_supported")

        # Entries are keyed by serial number, which needs a login to read. Match a
        # device that is already set up by its MAC address instead.
        mac = dr.format_mac(discovery_info.macaddress)
        if (entry := _entry_for_mac(self.hass, mac)) is not None:
            stored = URL(entry.data[CONF_HOST])
            if stored.host != discovery_info.ip:
                # The device got a new address: follow it, keeping scheme and port.
                self.hass.config_entries.async_update_entry(
                    entry,
                    data={**entry.data, CONF_HOST: str(stored.with_host(discovery_info.ip))},
                )
                self.hass.config_entries.async_schedule_reload(entry.entry_id)
            return self.async_abort(reason="already_configured")

        await self.async_set_unique_id(mac)
        self._abort_if_unique_id_configured()

        self._discovered_host = discovery_info.ip
        name = clean_text(public.get("device_name")) or "Teltonika NTP"
        self.context["title_placeholders"] = {"name": name, "host": discovery_info.ip}
        return await self.async_step_dhcp_confirm()

    async def async_step_dhcp_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for credentials for a discovered device."""
        errors: dict[str, str] = {}
        if user_input is not None and self._discovered_host:
            data = {**user_input, CONF_HOST: self._discovered_host, CONF_VERIFY_SSL: False}
            info = await self._async_try(data, errors)
            if info is not None:
                await self.async_set_unique_id(info["serial"], raise_on_progress=False)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=info["title"], data=_entry_data(info, data))

        return self.async_show_form(
            step_id="dhcp_confirm",
            data_schema=self.add_suggested_values_to_schema(CREDENTIALS_SCHEMA, user_input),
            errors=errors,
            description_placeholders=self.context.get("title_placeholders", {}),
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Start reauthentication."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for new credentials; the pinned certificate stays the same."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            data = {**entry.data, **user_input}
            info = await self._async_try(data, errors, pinned=entry.data.get(CONF_FINGERPRINT))
            if info is not None:
                await self.async_set_unique_id(info["serial"])
                self._abort_if_unique_id_mismatch(reason="wrong_device")
                return self.async_update_reload_and_abort(entry, data=_entry_data(info, data))

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self.add_suggested_values_to_schema(
                CREDENTIALS_SCHEMA, {CONF_USERNAME: entry.data.get(CONF_USERNAME)}
            ),
            errors=errors,
            description_placeholders={"name": entry.title, "host": entry.data[CONF_HOST]},
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the address or credentials; this also pins the current certificate."""
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            info = await self._async_try(user_input, errors)
            if info is not None:
                await self.async_set_unique_id(info["serial"])
                self._abort_if_unique_id_mismatch(reason="wrong_device")
                return self.async_update_reload_and_abort(entry, data=_entry_data(info, user_input))

        suggested = {
            CONF_HOST: entry.data.get(CONF_HOST),
            CONF_USERNAME: entry.data.get(CONF_USERNAME),
            CONF_VERIFY_SSL: entry.data.get(CONF_VERIFY_SSL, False),
        }
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(USER_SCHEMA, user_input or suggested),
            errors=errors,
        )
