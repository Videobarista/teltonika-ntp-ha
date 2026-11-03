"""Tests for the RutOS client and the helpers that treat device data as untrusted."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
import hashlib
import ssl
import time
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import ClientSession, Fingerprint, ServerFingerprintMismatch
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.teltonika_ntp import api
from custom_components.teltonika_ntp.api import (
    MAX_RESPONSE_BYTES,
    InvalidHostError,
    RutOSClient,
    TeltonikaAuthError,
    TeltonikaCertificateError,
    TeltonikaConnectionError,
    parse_host,
    valid_fingerprint,
)
from custom_components.teltonika_ntp.util import as_dict, clean_text

from .conftest import BASE_URL, FINGERPRINT, HOST

STATUS_URL = f"{BASE_URL}/api/unauthorized/status"
LOGIN_URL = f"{BASE_URL}/api/login"


@pytest.fixture
async def session(aioclient_mock: AiohttpClientMocker) -> AsyncGenerator[ClientSession]:
    """Return a client session answered by the aiohttp mocker."""
    client_session = aioclient_mock.create_session(asyncio.get_running_loop())
    yield client_session
    await client_session.close()


def _client(session: ClientSession, **kwargs) -> RutOSClient:
    kwargs.setdefault("fingerprint", FINGERPRINT)
    return RutOSClient(session, BASE_URL, "homeassistant", "secret", **kwargs)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (HOST, BASE_URL),
        (f"  {HOST}  ", BASE_URL),
        (f"{HOST}:8443", f"https://{HOST}:8443"),
        (f"https://{HOST}/", BASE_URL),
        (f"http://{HOST}", f"http://{HOST}"),
        ("ntp001.lan", "https://ntp001.lan"),
    ],
)
def test_parse_host_valid(value: str, expected: str) -> None:
    """Host names, IP addresses and base URLs are normalized; HTTPS is the default."""
    assert parse_host(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "exa mple",
        "line\nbreak",
        f"{HOST}/admin",
        f"user:pass@{HOST}",
        f"{HOST}?x=1",
        f"{HOST}#x",
        f"ftp://{HOST}",
        f"{HOST}:0",
        f"{HOST}:99999",
        "a" * 300,
    ],
)
def test_parse_host_invalid(value: str) -> None:
    """Anything but a plain host or base URL is refused."""
    with pytest.raises(InvalidHostError):
        parse_host(value)


def test_parse_host_non_string() -> None:
    """Non-text input is refused."""
    with pytest.raises(InvalidHostError):
        parse_host(None)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (FINGERPRINT, FINGERPRINT),
        (FINGERPRINT.upper(), FINGERPRINT),
        (":".join(FINGERPRINT[i : i + 2] for i in range(0, 64, 2)), FINGERPRINT),
        ("ab" * 31, None),
        ("zz" * 32, None),
        (None, None),
        (42, None),
    ],
)
def test_valid_fingerprint(value: object, expected: str | None) -> None:
    """Stored fingerprints are normalized, and corrupt ones are ignored."""
    assert valid_fingerprint(value) == expected


def test_clean_text_and_as_dict() -> None:
    """Device text is made printable and short; non-objects become empty dicts."""
    assert clean_text("a\nb\x1bc") == "a b c"
    assert clean_text("x" * 100, 10) == "x" * 9 + "…"
    assert clean_text("   ") is None
    assert clean_text(True) is None
    assert clean_text(["x"]) is None
    assert clean_text(12) == "12"
    assert as_dict({"a": 1}) == {"a": 1}
    assert as_dict(["a"]) == {}


def test_tls_settings() -> None:
    """HTTP uses no TLS, a pinned entry pins, and CA verification verifies."""
    session = MagicMock()
    assert RutOSClient(session, f"http://{HOST}", "u", "p")._ssl is False
    assert RutOSClient(session, BASE_URL, "u", "p", verify_ssl=True)._ssl is True
    pinned = RutOSClient(session, BASE_URL, "u", "p", fingerprint=FINGERPRINT)._ssl
    assert isinstance(pinned, Fingerprint)
    assert RutOSClient(session, BASE_URL, "u", "p", fingerprint="corrupt")._ssl is False


async def test_public_info(aioclient_mock: AiohttpClientMocker, session: ClientSession) -> None:
    """Public status is read and "N/A" becomes None."""
    aioclient_mock.get(
        STATUS_URL, json={"success": True, "data": {"device_name": "NTP001", "x": "N/A"}}
    )
    assert await _client(session).async_get_public_info() == {"device_name": "NTP001", "x": None}


async def test_response_size_limit(
    aioclient_mock: AiohttpClientMocker, session: ClientSession
) -> None:
    """A reply over 1 MB is refused instead of being read into memory."""
    aioclient_mock.get(STATUS_URL, content=b" " * (MAX_RESPONSE_BYTES + 1))
    with pytest.raises(TeltonikaConnectionError, match="larger than"):
        await _client(session).async_get_public_info()


@pytest.mark.parametrize(
    "body",
    [b"not json", b"[" * 100_000, b'["a", "list"]', b"", b'{"success": true, "data": "x"}'],
)
async def test_unusable_replies(
    aioclient_mock: AiohttpClientMocker, session: ClientSession, body: bytes
) -> None:
    """Invalid, deeply nested or wrongly typed JSON is a connection error, not a crash."""
    aioclient_mock.get(STATUS_URL, content=body)
    with pytest.raises(TeltonikaConnectionError):
        await _client(session).async_get_public_info()


async def test_certificate_mismatch(
    aioclient_mock: AiohttpClientMocker, session: ClientSession
) -> None:
    """A certificate other than the pinned one is reported as such."""
    aioclient_mock.get(STATUS_URL, exc=ServerFingerprintMismatch(b"a" * 32, b"b" * 32, HOST, 443))
    with pytest.raises(TeltonikaCertificateError):
        await _client(session).async_get_public_info()


@pytest.mark.parametrize("exc", [TimeoutError(), OSError("refused")])
async def test_connection_errors(
    aioclient_mock: AiohttpClientMocker, session: ClientSession, exc: Exception
) -> None:
    """Network problems become connection errors."""
    aioclient_mock.get(STATUS_URL, exc=exc)
    with pytest.raises(TeltonikaConnectionError):
        await _client(session).async_get_public_info()


async def test_no_login_without_pin(
    aioclient_mock: AiohttpClientMocker, session: ClientSession
) -> None:
    """Over HTTPS without a pin or CA verification the password is never sent."""
    client = RutOSClient(session, BASE_URL, "homeassistant", "secret")
    with pytest.raises(TeltonikaCertificateError):
        await client.async_login()
    assert aioclient_mock.call_count == 0


async def test_login_over_explicit_http(
    aioclient_mock: AiohttpClientMocker, session: ClientSession
) -> None:
    """A user-chosen http:// address may log in without a pin."""
    aioclient_mock.post(
        f"http://{HOST}/api/login",
        json={"success": True, "data": {"token": "abc", "expires": 300}},
    )
    client = RutOSClient(session, f"http://{HOST}", "homeassistant", "secret")
    await client.async_login()
    assert client._token == "abc"


@pytest.mark.parametrize(
    "token", ["", "with space", "line\r\nX-Injected: 1", "x" * 5000, 12345, None]
)
async def test_unsafe_token_refused(
    aioclient_mock: AiohttpClientMocker, session: ClientSession, token: object
) -> None:
    """Tokens that could break or inject HTTP headers are refused."""
    aioclient_mock.post(LOGIN_URL, json={"success": True, "data": {"token": token}})
    with pytest.raises(TeltonikaConnectionError, match="token"):
        await _client(session).async_login()


@pytest.mark.parametrize(
    ("body", "max_lifetime"),
    [
        (b'{"success": true, "data": {"token": "abc", "expires": 1e12}}', 3600),
        (b'{"success": true, "data": {"token": "abc", "expires": NaN}}', 299),
        (b'{"success": true, "data": {"token": "abc", "expires": -5}}', 299),
        (b'{"success": true, "data": {"token": "abc", "expires": "soon"}}', 299),
        (b'{"success": true, "data": {"token": "abc", "expires": true}}', 299),
    ],
)
async def test_token_lifetime_is_bounded(
    aioclient_mock: AiohttpClientMocker, session: ClientSession, body: bytes, max_lifetime: int
) -> None:
    """Absurd expiry values cannot keep a token alive for ever."""
    aioclient_mock.post(LOGIN_URL, content=body)
    client = _client(session)
    before = time.monotonic()
    await client.async_login()
    assert client._token_valid_until - before <= max_lifetime


async def test_login_error_text_is_cleaned(
    aioclient_mock: AiohttpClientMocker, session: ClientSession
) -> None:
    """Error text from the device cannot inject log lines and is kept short."""
    aioclient_mock.post(
        LOGIN_URL,
        status=401,
        json={"success": False, "errors": [{"code": 121, "error": "bad\n\x1b[31m" + "x" * 500}]},
    )
    with pytest.raises(TeltonikaAuthError) as err:
        await _client(session).async_login()
    assert "\n" not in str(err.value) and "\x1b" not in str(err.value)
    assert len(str(err.value)) <= 200


async def test_login_odd_error_shapes(
    aioclient_mock: AiohttpClientMocker, session: ClientSession
) -> None:
    """Error lists in unexpected shapes are handled."""
    aioclient_mock.post(LOGIN_URL, json={"success": False, "errors": {"code": True}})
    with pytest.raises(TeltonikaConnectionError, match="Unknown API error"):
        await _client(session).async_login()


async def test_session_renewed_once(
    aioclient_mock: AiohttpClientMocker, session: ClientSession
) -> None:
    """A rejected session logs in again once; a second rejection is an auth error."""
    aioclient_mock.post(LOGIN_URL, json={"success": True, "data": {"token": "abc"}})
    aioclient_mock.get(f"{BASE_URL}/api/system/device/status", status=401, json={})
    with pytest.raises(TeltonikaAuthError):
        await _client(session).async_get_device_status()
    logins = [call for call in aioclient_mock.mock_calls if str(call[1]) == LOGIN_URL]
    assert len(logins) == 2


async def test_logout_failure_is_logged(
    aioclient_mock: AiohttpClientMocker, session: ClientSession, caplog: pytest.LogCaptureFixture
) -> None:
    """A failing logout does not raise."""
    aioclient_mock.post(LOGIN_URL, json={"success": True, "data": {"token": "abc"}})
    aioclient_mock.post(f"{BASE_URL}/api/logout", exc=OSError("gone"))
    client = _client(session)
    await client.async_login()
    with caplog.at_level("DEBUG"):
        await client.async_logout()
    assert client._token is None
    assert "Logout" in caplog.text


def _fake_tls_writer(der: bytes | None, close_error: Exception | None = None) -> MagicMock:
    ssl_object = MagicMock()
    ssl_object.getpeercert.return_value = der
    writer = MagicMock()
    writer.get_extra_info.return_value = ssl_object
    writer.wait_closed = AsyncMock(side_effect=close_error)
    return writer


async def test_fetch_certificate_fingerprint() -> None:
    """The SHA-256 of the presented certificate is returned and the socket closed."""
    writer = _fake_tls_writer(b"certificate")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    with patch.object(
        api.asyncio, "open_connection", AsyncMock(return_value=(MagicMock(), writer))
    ) as open_connection:
        result = await api.async_fetch_certificate_fingerprint(f"{BASE_URL}:8443", context)

    assert result == hashlib.sha256(b"certificate").hexdigest()
    assert open_connection.await_args.args[:2] == (HOST, 8443)
    writer.close.assert_called_once()


async def test_fetch_certificate_close_error_is_tolerated() -> None:
    """A failing TLS close after reading the certificate is only logged."""
    writer = _fake_tls_writer(b"certificate", ssl.SSLError("close"))
    with patch.object(api.asyncio, "open_connection", AsyncMock(return_value=(None, writer))):
        result = await api.async_fetch_certificate_fingerprint(BASE_URL, MagicMock())
    assert result == hashlib.sha256(b"certificate").hexdigest()


@pytest.mark.parametrize(
    "open_result",
    [OSError("refused"), TimeoutError(), (None, _fake_tls_writer(None))],
)
async def test_fetch_certificate_fingerprint_errors(open_result: object) -> None:
    """No connection or no certificate is a connection error."""
    mock = (
        AsyncMock(side_effect=open_result)
        if isinstance(open_result, Exception)
        else AsyncMock(return_value=open_result)
    )
    with (
        patch.object(api.asyncio, "open_connection", mock),
        pytest.raises(TeltonikaConnectionError),
    ):
        await api.async_fetch_certificate_fingerprint(BASE_URL, MagicMock())
