"""Small client for the RutOS REST API used by Teltonika devices.

The login and session handling follow the approach of the teltasync library by
Karl Beecken (Apache License 2.0), which powers the core "teltonika" integration.
It is reimplemented here with plain aiohttp so this custom integration does not pin
a library version that Home Assistant core also depends on. ``parse_host`` is based on
``normalize_url`` in the util module of the Home Assistant core Teltonika integration
(Apache License 2.0).

Security notes:

- Credentials are only sent over HTTPS to a certificate that is either verified by a
  trusted CA or pinned by its SHA-256 fingerprint (trust on first use), or over plain
  HTTP when the user explicitly configured an address with the http scheme.
- Everything the device returns is treated as untrusted: bodies are size-limited,
  JSON types are checked and text is cleaned before it is used or logged.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import hashlib
import json
import logging
import math
import ssl
import time
from typing import Any

from aiohttp import (
    ClientError,
    ClientSession,
    ClientTimeout,
    Fingerprint,
    ServerFingerprintMismatch,
)
from yarl import URL

from .const import API_TIMEOUT
from .util import MAX_ERROR_LENGTH, clean_text

_LOGGER = logging.getLogger(__name__)

# RutOS error codes, see the Teltonika API documentation.
_AUTH_ERROR_CODES = {120, 121, 122, 123}
_SESSION_REJECTED_CODES = {120, 123}

# Hard limits for data coming from the device.
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_TOKEN_LENGTH = 4096
_TOKEN_LIFETIME_DEFAULT = 299.0
_TOKEN_LIFETIME_MAX = 3600.0

MAX_HOST_LENGTH = 253
# Longest accepted input: a host plus scheme, port and trailing slash
# ("https" + "://" + ":65535" + "/" is 15 characters).
MAX_INPUT_LENGTH = MAX_HOST_LENGTH + 15


class TeltonikaError(Exception):
    """Base error for the RutOS client."""


class TeltonikaConnectionError(TeltonikaError):
    """The device could not be reached or sent an unusable response."""


class TeltonikaCertificateError(TeltonikaConnectionError):
    """The device presented another TLS certificate than the pinned one."""


class TeltonikaAuthError(TeltonikaError):
    """The device rejected the credentials."""


class TeltonikaApiError(TeltonikaError):
    """The device answered with an API error (for example an unknown endpoint)."""

    def __init__(self, message: str, code: int | None = None) -> None:
        """Store the RutOS error code alongside the message."""
        super().__init__(message)
        self.code = code


class InvalidHostError(ValueError):
    """The address entered by the user is not a plain host, IP address or base URL."""


def parse_host(host: str) -> str:
    """Validate user input and return ``scheme://host[:port]``.

    Accepts a host name or IP address, optionally with a port, or a base URL with the
    http or https scheme. Without a scheme HTTPS is used; HTTP is only used when the
    user typed it. Paths, queries, fragments and credentials in the
    address are refused.
    """
    value = host.strip() if isinstance(host, str) else ""
    if not value or len(value) > MAX_INPUT_LENGTH:
        raise InvalidHostError("empty or too long")
    if any(char.isspace() or not char.isprintable() for char in value):
        raise InvalidHostError("contains whitespace or control characters")
    if "://" not in value:
        value = f"https://{value}"
    try:
        url = URL(value)
        port = url.port
    except ValueError as err:
        raise InvalidHostError(str(err)) from err
    if url.scheme not in ("http", "https"):
        raise InvalidHostError("only http and https are supported")
    if not url.host or len(url.host) > MAX_HOST_LENGTH:
        raise InvalidHostError("missing or too long host")
    if url.user or url.password or url.query_string or url.fragment:
        raise InvalidHostError("credentials, query or fragment in address")
    if url.path not in ("", "/"):
        raise InvalidHostError("path in address")
    if port is None or not 0 < port < 65536:
        raise InvalidHostError("invalid port")
    return str(url.origin())


def is_https(base_url: str) -> bool:
    """Return True when the base URL uses HTTPS."""
    return URL(base_url).scheme == "https"


def hostname_from_url(base_url: str) -> str:
    """Return the bare host name or IP address of a base URL."""
    return URL(base_url).host or base_url


async def async_fetch_certificate_fingerprint(
    base_url: str, ssl_context: ssl.SSLContext, timeout: float = API_TIMEOUT
) -> str:
    """Return the SHA-256 fingerprint (hex) of the certificate the device presents.

    ``ssl_context`` must not verify the certificate: this is how a self-signed
    certificate is pinned on first use. No credentials are sent on this connection.
    """
    url = URL(base_url)
    host = url.host or ""
    port = url.port or 443
    try:
        async with asyncio.timeout(timeout):
            _reader, writer = await asyncio.open_connection(
                host, port, ssl=ssl_context, server_hostname=host
            )
    except (TimeoutError, OSError) as err:
        raise TeltonikaConnectionError(f"Cannot open TLS connection to {host}:{port}") from err

    try:
        ssl_object = writer.get_extra_info("ssl_object")
        der = ssl_object.getpeercert(binary_form=True) if ssl_object is not None else None
    finally:
        writer.close()
        try:
            async with asyncio.timeout(2):
                await writer.wait_closed()
        except (TimeoutError, OSError, ssl.SSLError) as err:
            _LOGGER.debug("Closing TLS probe to %s:%s failed: %s", host, port, err)

    if not der:
        raise TeltonikaConnectionError(f"{host}:{port} did not present a certificate")
    return hashlib.sha256(der).hexdigest()


def valid_fingerprint(value: Any) -> str | None:
    """Return a normalized SHA-256 fingerprint (64 lowercase hex characters) or None."""
    if not isinstance(value, str):
        return None
    candidate = value.replace(":", "").strip().lower()
    if len(candidate) != 64 or any(char not in "0123456789abcdef" for char in candidate):
        return None
    return candidate


def _clean(value: Any) -> Any:
    """Turn the "N/A" strings RutOS uses for missing values into None."""
    if value == "N/A":
        return None
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clean(item) for item in value]
    return value


def _parse_json(raw: bytes) -> dict[str, Any] | None:
    """Decode a JSON object; anything else (invalid, too deep, not an object) is None."""
    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    return payload if isinstance(payload, dict) else None


def _first_error(payload: Mapping[str, Any]) -> tuple[int | None, str]:
    """Return the code and cleaned text of the first error in an API reply."""
    errors = payload.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        code = errors[0].get("code")
        text = clean_text(errors[0].get("error"), MAX_ERROR_LENGTH) or ""
        return (code if isinstance(code, int) and not isinstance(code, bool) else None, text)
    return None, "Unknown API error"


def _valid_token(value: Any) -> str | None:
    """Return the token if it is safe to send back in an Authorization header."""
    if not isinstance(value, str) or not 0 < len(value) <= MAX_TOKEN_LENGTH:
        return None
    if not all(33 <= ord(char) < 127 for char in value):
        return None
    return value


def _token_lifetime(value: Any) -> float:
    """Return a sane token lifetime in seconds from the device's ``expires`` field."""
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return _TOKEN_LIFETIME_DEFAULT
    if value <= 0:
        return _TOKEN_LIFETIME_DEFAULT
    return min(float(value), _TOKEN_LIFETIME_MAX)


class RutOSClient:
    """Authenticated client for one Teltonika device."""

    def __init__(
        self,
        session: ClientSession,
        base_url: str,
        username: str,
        password: str,
        *,
        verify_ssl: bool = False,
        fingerprint: str | None = None,
    ) -> None:
        """Initialize the client. ``base_url`` is ``scheme://host`` without /api.

        For HTTPS without CA verification, ``fingerprint`` is the pinned SHA-256
        certificate fingerprint; without it the client can only read public data.
        """
        self._session = session
        self._api = f"{base_url}/api"
        self._username = username
        self._password = password
        self._https = is_https(base_url)
        self._verify_ssl = verify_ssl
        self._fingerprint = valid_fingerprint(fingerprint)
        self._token: str | None = None
        self._token_valid_until = 0.0
        self._lock = asyncio.Lock()

    @property
    def base_url(self) -> str:
        """Return the device base URL."""
        return self._api.removesuffix("/api")

    @property
    def _ssl(self) -> bool | Fingerprint:
        """Return the TLS setting for aiohttp."""
        if not self._https:
            return False
        if self._verify_ssl:
            return True
        if self._fingerprint:
            return Fingerprint(bytes.fromhex(self._fingerprint))
        return False

    @property
    def _may_send_credentials(self) -> bool:
        """Return True when the connection is trusted enough for the password."""
        return not self._https or self._verify_ssl or bool(self._fingerprint)

    async def _open(
        self, method: str, endpoint: str, *, headers: dict[str, str] | None = None, **kwargs: Any
    ) -> tuple[int, dict[str, Any] | None]:
        """Send one request and return the HTTP status and JSON object (or None)."""
        try:
            async with self._session.request(
                method,
                f"{self._api}/{endpoint.lstrip('/')}",
                headers=headers,
                ssl=self._ssl,
                timeout=ClientTimeout(total=API_TIMEOUT),
                allow_redirects=False,
                **kwargs,
            ) as resp:
                status = resp.status
                body = bytearray()
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise TeltonikaConnectionError(
                            f"Reply from {endpoint} is larger than {MAX_RESPONSE_BYTES} bytes"
                        )
        except ServerFingerprintMismatch as err:
            raise TeltonikaCertificateError(
                f"{self.base_url} presented another certificate than the pinned one"
            ) from err
        except TimeoutError as err:
            raise TeltonikaConnectionError(f"Timeout talking to {self._api}") from err
        except (ClientError, OSError) as err:
            raise TeltonikaConnectionError(f"Cannot connect to {self._api}: {err}") from err
        return status, _parse_json(bytes(body))

    async def async_get_public_info(self) -> dict[str, Any]:
        """Return the device data RutOS exposes without logging in."""
        status, payload = await self._open("GET", "unauthorized/status")
        if not payload or not payload.get("success") or not isinstance(payload.get("data"), dict):
            raise TeltonikaConnectionError(
                f"Unexpected reply from unauthorized/status (HTTP {status})"
            )
        return _clean(payload["data"])

    async def async_login(self) -> None:
        """Log in and cache the session token."""
        if not self._may_send_credentials:
            raise TeltonikaCertificateError(
                "Refusing to send credentials: the device certificate is neither verified "
                "nor pinned"
            )
        status, payload = await self._open(
            "POST",
            "login",
            json={"username": self._username, "password": self._password},
        )
        if payload is None:
            raise TeltonikaConnectionError(f"Unexpected non-JSON login reply (HTTP {status})")

        data = payload.get("data")
        if payload.get("success") and isinstance(data, dict):
            token = _valid_token(data.get("token"))
            if token is None:
                raise TeltonikaConnectionError("Login reply contains no usable token")
            self._token = token
            lifetime = _token_lifetime(data.get("expires"))
            # Renew a few seconds early so a request never races the expiry.
            self._token_valid_until = time.monotonic() + max(lifetime - 10.0, 5.0)
            return

        code, message = _first_error(payload)
        if status == 401 or code in _AUTH_ERROR_CODES:
            raise TeltonikaAuthError(message or "Invalid username or password")
        raise TeltonikaConnectionError(f"Login failed: {message} (code {code})")

    async def async_logout(self) -> None:
        """End the session on the device. Errors are logged, not raised."""
        if self._token is None:
            return
        token, self._token = self._token, None
        self._token_valid_until = 0.0
        try:
            await self._open("POST", "logout", headers={"Authorization": f"Bearer {token}"})
        except TeltonikaConnectionError as err:
            _LOGGER.debug("Logout from %s failed: %s", self._api, err)

    async def _ensure_token(self) -> str:
        """Return a valid token, logging in when needed."""
        async with self._lock:
            if self._token is None or time.monotonic() >= self._token_valid_until:
                await self.async_login()
            if self._token is None:
                raise TeltonikaAuthError("No session token after login")
            return self._token

    async def async_get(self, endpoint: str) -> Any:
        """GET an authenticated endpoint and return its ``data`` field."""
        token = await self._ensure_token()
        status, payload = await self._open(
            "GET", endpoint, headers={"Authorization": f"Bearer {token}"}
        )

        if self._session_rejected(status, payload):
            # The device forgot our session (reboot, timeout): log in once more.
            self._token = None
            token = await self._ensure_token()
            status, payload = await self._open(
                "GET", endpoint, headers={"Authorization": f"Bearer {token}"}
            )
            if self._session_rejected(status, payload):
                self._token = None
                raise TeltonikaAuthError("Session rejected after logging in again")

        if payload is None:
            raise TeltonikaConnectionError(f"Unexpected reply from {endpoint} (HTTP {status})")
        if not payload.get("success"):
            code, message = _first_error(payload)
            if code in _AUTH_ERROR_CODES:
                raise TeltonikaAuthError(message)
            raise TeltonikaApiError(f"{endpoint}: {message}", code)
        return _clean(payload.get("data"))

    @staticmethod
    def _session_rejected(status: int, payload: dict[str, Any] | None) -> bool:
        """Return True when the device no longer accepts our token."""
        if status == 401:
            return True
        if payload is not None and not payload.get("success"):
            code, _ = _first_error(payload)
            return code in _SESSION_REJECTED_CODES
        return False

    async def async_get_device_status(self) -> dict[str, Any]:
        """Return manufacturing, firmware and model information."""
        data = await self.async_get("system/device/status")
        if not isinstance(data, dict):
            raise TeltonikaConnectionError("system/device/status returned no data")
        return data
