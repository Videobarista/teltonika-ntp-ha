"""The integration only talks to the configured NTP server.

The runtime half of the "local only" badge: the real RutOS client, certificate
pinning and SNTP client run against a fake NTP001 on a non-default web port, and
every HTTP request, TLS connection and UDP query made during the config flow,
setup, polling, a manual refresh, reauthentication, reconfiguration and unload is
recorded. All of them must go to the configured host: HTTP and TLS to the
configured port, NTP to the device's NTP port. Real network access is blocked in
the tests anyway, so anything else would fail. The integration has no actions or
writable entities, so it never sends commands to the device.

The static half is scripts/check_local_only.py, which is tested here as well.
"""

from __future__ import annotations

from datetime import timedelta
import importlib.util
import json
from pathlib import Path
import shutil
import struct
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from pytest_homeassistant_custom_component.common import async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker
from yarl import URL

from custom_components.teltonika_ntp import api, ntp
from custom_components.teltonika_ntp.const import DOMAIN, NTP_PORT
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    SOURCE_RECONFIGURE,
    SOURCE_USER,
    ConfigEntryState,
)
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from .conftest import DEVICE_STATUS, HOST, PUBLIC_INFO

ROOT = Path(__file__).resolve().parents[1]
PORT = 8443
API = f"https://{HOST}:{PORT}/api"
CREDENTIALS = {CONF_USERNAME: "homeassistant", CONF_PASSWORD: "secret"}
_PACKET = struct.Struct("!BBbbII4sQQQQ")


def _load_checker() -> Any:
    spec = importlib.util.spec_from_file_location(
        "check_local_only", ROOT / "scripts" / "check_local_only.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ntp_reply(request: bytes) -> bytes:
    """Answer an SNTP request like a synchronized stratum 1 GPS server."""
    origin = _PACKET.unpack(request)[-1]
    now = ntp._to_ntp(time.time())
    return _PACKET.pack((4 << 3) | 4, 1, 4, -20, 0, 76, b"GPS\x00", now, origin, now, now)


class FakeDevice:
    """A fake NTP001 that records every TLS and UDP connection made to it."""

    def __init__(self) -> None:
        self.tls: list[tuple[str, int]] = []
        self.udp: list[tuple[str, int]] = []
        self.ntp_down = False

    def serve_api(self, aioclient_mock: AiohttpClientMocker) -> None:
        """Answer the RutOS API on the configured port."""
        aioclient_mock.get(
            f"{API}/unauthorized/status", json={"success": True, "data": PUBLIC_INFO}
        )
        aioclient_mock.post(
            f"{API}/login", json={"success": True, "data": {"token": "abc", "expires": 300}}
        )
        aioclient_mock.get(
            f"{API}/system/device/status", json={"success": True, "data": DEVICE_STATUS}
        )
        aioclient_mock.post(f"{API}/logout", json={"success": True})

    async def open_connection(self, host: str, port: int, **kwargs: Any) -> tuple[Any, Any]:
        """Stand in for asyncio.open_connection: the TLS certificate probe."""
        self.tls.append((host, port))
        ssl_object = MagicMock()
        ssl_object.getpeercert.return_value = b"device certificate"
        writer = MagicMock()
        writer.get_extra_info.return_value = ssl_object
        writer.wait_closed = AsyncMock()
        return MagicMock(), writer

    async def create_endpoint(self, loop: Any, factory: Any, host: str, port: int) -> Any:
        """Stand in for the SNTP socket: reply to the request the client sends."""
        self.udp.append((host, port))
        if self.ntp_down:
            raise OSError("network unreachable")
        protocol = factory()
        transport = MagicMock()
        transport.sendto.side_effect = lambda data: loop.call_soon(
            protocol.datagram_received, _ntp_reply(data), (host, port)
        )
        return transport


async def test_all_traffic_goes_to_the_ntp_server(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, mock_fingerprint: AsyncMock
) -> None:
    """Config flow, setup, polling, refresh, reauth, reconfigure and unload stay local."""
    device = FakeDevice()
    device.serve_api(aioclient_mock)
    # Use the real certificate probe (its socket is the fake device).
    mock_fingerprint.side_effect = api.async_fetch_certificate_fingerprint
    assert await async_setup_component(hass, "homeassistant", {})

    with (
        patch.object(api.asyncio, "open_connection", device.open_connection),
        patch.object(ntp, "_create_endpoint", device.create_endpoint),
    ):
        # Config flow, which also sets the entry up.
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_HOST: f"{HOST}:{PORT}", **CREDENTIALS, CONF_VERIFY_SSL: False},
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()
        entry = result["result"]
        assert entry.state is ConfigEntryState.LOADED

        # Polling, including one round where NTP does not answer and the API is asked.
        for minutes, ntp_down in ((1, False), (2, True), (3, False)):
            device.ntp_down = ntp_down
            async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=minutes))
            await hass.async_block_till_done()

        # A manual refresh from the UI.
        await hass.services.async_call(
            "homeassistant",
            "update_entity",
            {"entity_id": "sensor.ntp001_synchronization"},
            blocking=True,
        )

        # Reauthentication (keeps the pin) and reconfiguration (pins again).
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id}, data=entry.data
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
        assert result["reason"] == "reauth_successful"
        await hass.async_block_till_done()

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_HOST: f"{HOST}:{PORT}", **CREDENTIALS, CONF_VERIFY_SSL: False},
        )
        assert result["reason"] == "reconfigure_successful"
        await hass.async_block_till_done()

        assert await hass.config_entries.async_unload(entry.entry_id)

    http_urls = [URL(str(call[1])) for call in aioclient_mock.mock_calls]
    assert http_urls, "the test made no HTTP requests"
    assert device.tls, "the test made no TLS connections"
    assert device.udp, "the test made no NTP queries"
    assert {(url.scheme, url.host, url.port) for url in http_urls} == {("https", HOST, PORT)}
    assert set(device.tls) == {(HOST, PORT)}
    assert set(device.udp) == {(HOST, NTP_PORT)}
    # Every part of the run was covered: logins, polling via the API, and logout.
    paths = [url.path for url in http_urls]
    assert paths.count("/api/unauthorized/status") >= 4
    assert "/api/login" in paths
    assert "/api/logout" in paths


async def test_sntp_socket_is_connected_to_the_server() -> None:
    """The SNTP socket is connected to the server only, never bound to listen."""
    loop = MagicMock()
    transport = MagicMock()
    loop.create_datagram_endpoint = AsyncMock(return_value=(transport, MagicMock()))

    assert await ntp._create_endpoint(loop, MagicMock(), HOST, NTP_PORT) is transport

    call = loop.create_datagram_endpoint.await_args
    assert call.kwargs == {"remote_addr": (HOST, NTP_PORT)}


def test_static_check_passes() -> None:
    """The integration passes scripts/check_local_only.py."""
    checker = _load_checker()
    assert checker.main(["check_local_only.py"]) == 0


def test_static_check_catches_leaks(tmp_path: Path) -> None:
    """The check fails on an integration that could send data elsewhere."""
    checker = _load_checker()
    leaky = tmp_path / "leaky"
    shutil.copytree(ROOT / "custom_components" / "teltonika_ntp", leaky)
    manifest = json.loads((leaky / "manifest.json").read_text())
    manifest.update(
        iot_class="cloud_polling", requirements=["phonehome==1.0"], dependencies=["mqtt"]
    )
    (leaky / "manifest.json").write_text(json.dumps(manifest))
    (leaky / "telemetry.py").write_text(
        "import aiohttp\n"
        "import requests\n"
        "from aiohttp import ClientSession\n"
        "from homeassistant.helpers.aiohttp_client import async_get_clientsession\n\n\n"
        "async def leak(hass, loop, transport):\n"
        "    session = async_get_clientsession(hass)\n"
        "    await session.post('https://telemetry.example.com/collect')\n"
        "    await aiohttp.request('GET', 'x')\n"
        "    ClientSession()\n"
        "    await loop.create_datagram_endpoint(None, local_addr=('0.0.0.0', 9999))\n"
        "    transport.sendto(b'x', ('8.8.8.8', 53))\n"
        "    await hass.services.async_call('notify', 'notify', {})\n"
        "    return 'send to 2001:4860:4860::8888 or analytics.vendor.io'\n"
    )
    # Turn the SNTP socket into a listener as well.
    sntp = (leaky / "ntp.py").read_text()
    (leaky / "ntp.py").write_text(
        sntp.replace("remote_addr=(host, port))", "remote_addr=(host, port), local_addr=('', 0))")
    )

    problems = checker.check_manifest(leaky)
    for name in ("telemetry.py", "ntp.py"):
        problems += checker.check_source(leaky / name, f"leaky/{name}")
    text = "\n".join(problems)
    for expected in (
        "iot_class",
        "depends on 'mqtt'",
        "phonehome",
        "imports requests",
        "async_get_clientsession() outside",
        "session.post() outside",
        "aiohttp.request() bypasses",
        "ClientSession() bypasses",
        "create_datagram_endpoint() outside",
        "not a connected UDP socket",
        "sendto() to an explicit address",
        "calls a service of another integration",
        "https://telemetry.example.com",
        "8.8.8.8",
        "2001:4860:4860::8888",
        "analytics.vendor.io",
    ):
        assert expected in text, expected
    assert checker.main(["check_local_only.py", str(leaky)]) == 1
