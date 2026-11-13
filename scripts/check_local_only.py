"""Check that a Home Assistant integration only talks to its own device.

Runs with the Python standard library only (no installs), on Python 3.10+.
It reads the integration's manifest and source code and fails when it finds
anything that could send data elsewhere:

1. Manifest: the IoT class is local, no cloud, webhook or MQTT dependencies, and
   no Python requirements other than the ones explicitly allowed below.
2. No listeners: nothing opens a server, webhook, HTTP view or unconnected UDP
   socket.
3. Connections only where allowed: each file below may use only the connection
   calls listed for it, through Home Assistant's own HTTP session. No other
   network libraries, no private HTTP sessions, no UDP datagrams to a fixed
   address and no service calls to other integrations (notify, MQTT, ...).
4. No hard-coded internet addresses: no URLs with a host, public host names or
   public IPv4/IPv6 addresses in the code; every address comes from the
   configuration.

What it does not cover: Home Assistant itself, and the code of an allowed
requirement. The runtime side (all traffic goes to the configured host and
port) is checked by the tests in tests/test_local_only.py.

Usage: python3 scripts/check_local_only.py [path/to/custom_components/<domain>]
"""

from __future__ import annotations

import ast
import ipaddress
import json
from pathlib import Path
import re
import sys

# ---------------------------------------------------------------------------
# Settings for this integration (Teltonika NTP Server)
# ---------------------------------------------------------------------------
# The integration talks to one device in three ways, all to the configured host:
#   - the RutOS REST API over HTTPS (or HTTP if the user typed http://), through
#     Home Assistant's shared aiohttp session, on the configured port;
#   - one short TLS connection to that same host and port that reads the device
#     certificate so it can be pinned (no data is sent on it);
#   - SNTP: one UDP datagram per poll to the device's NTP port (123). This is the
#     only traffic that does not use the configured web port.
#
# Files that may open connections, and the only connection calls each may use.
CONNECTION_FILES: dict[str, set[str]] = {
    "api.py": {"session.request", "open_connection"},
    "ntp.py": {"create_datagram_endpoint"},
}
# Files that may fetch Home Assistant's shared HTTP session (and pass it to the
# client in api.py). They make no requests themselves.
SESSION_FILES = {"__init__.py", "config_flow.py"}
# Files where UDP is allowed, and then only as a socket *connected* to one remote
# address (remote_addr=..., no local_addr, broadcast, port reuse or ready-made
# socket). The OS then only delivers replies from that address, so this is an
# outgoing query, not a listener. Used by ntp.py for the SNTP query to the device.
CONNECTED_UDP_FILES = {"ntp.py"}
# Names of an own client object whose methods may open connections from other
# files (none: only api.py and ntp.py connect).
CLIENT_NAMES: set[str] = set()
# Python requirements that are allowed (none: the integration needs no packages).
ALLOWED_REQUIREMENTS: set[str] = set()
# Literal addresses that are allowed in the code (none).
ALLOWED_ADDRESSES: set[str] = set()

# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------
FORBIDDEN_DEPENDENCIES = {"cloud", "mqtt", "webhook"}
# Modules that talk to the network on their own, serve, or report elsewhere.
FORBIDDEN_MODULES = {
    "aiodns",
    "aiohttp.request",
    "aiohttp.web",
    "dns",
    "ftplib",
    "grpc",
    "http.client",
    "http.server",
    "httpx",
    "paho",
    "pycurl",
    "requests",
    "sentry_sdk",
    "smtplib",
    "socket",
    "socketserver",
    "telnetlib",
    "urllib.request",
    "urllib3",
    "websockets",
    "xmlrpc",
    "zeroconf",
    "homeassistant.components.cloud",
    "homeassistant.components.http",
    "homeassistant.components.mqtt",
    "homeassistant.components.webhook",
    "homeassistant.helpers.httpx_client",
}
# Calls that listen for incoming connections.
LISTEN_CALLS = {
    "create_server",
    "create_unix_server",
    "register_view",
    "start_server",
    "start_unix_server",
}
# Calls that open outgoing connections.
CONNECT_CALLS = {
    "create_connection",
    "create_datagram_endpoint",
    "open_connection",
    "open_unix_connection",
    "sock_connect",
    "ws_connect",
}
# Arguments that turn a UDP endpoint into a listener or a broadcaster.
UDP_LISTEN_ARGUMENTS = {"allow_broadcast", "local_addr", "reuse_port", "sock"}
SESSION_METHODS = {"delete", "get", "head", "options", "patch", "post", "put", "request"}
SESSION_FACTORIES = {"async_create_clientsession", "async_get_clientsession"}
# Building an own HTTP session bypasses Home Assistant's shared one.
PRIVATE_SESSION_CALLS = {"ClientSession", "TCPConnector", "UnixConnector"}

_URL = re.compile(r"\b(?:https?|wss?|ftp|mqtts?|tcp|udp)://([^\s/'\"{}]+)", re.IGNORECASE)
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_IPV6 = re.compile(r"(?<![\w:])[0-9a-f]{0,4}(?::[0-9a-f]{0,4}){2,7}(?![\w:])", re.IGNORECASE)
_TLDS = "com|net|org|io|dev|app|cloud|ai|co|eu|nl|de|be|uk|fr|info|biz|me|tv|us"
_HOSTNAME = re.compile(rf"\b(?:[a-z0-9-]+\.)+(?:{_TLDS})\b", re.IGNORECASE)
# Example and local addresses never leave the network.
_HARMLESS_NETWORKS = [
    ipaddress.ip_network(net)
    for net in (
        "0.0.0.0/8",
        "127.0.0.0/8",
        "192.0.2.0/24",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "::/128",
        "::1/128",
        "2001:db8::/32",
    )
]


def _harmless_ip(text: str) -> bool:
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return True  # Not an address (a version number or a time, for instance).
    return any(address in network for network in _HARMLESS_NETWORKS)


def _call_name(node: ast.Call) -> tuple[str, str]:
    """Return (attribute or function name, name of the object it is called on)."""
    func = node.func
    if isinstance(func, ast.Attribute):
        owner = func.value
        if isinstance(owner, ast.Attribute):
            return func.attr, owner.attr
        if isinstance(owner, ast.Name):
            return func.attr, owner.id
        return func.attr, ""
    if isinstance(func, ast.Name):
        return func.id, ""
    return "", ""


def _allowed_calls(path: Path) -> set[str]:
    return CONNECTION_FILES.get(path.name, set())


def check_manifest(integration: Path) -> list[str]:
    """Check the manifest for cloud use and extra requirements."""
    manifest = json.loads((integration / "manifest.json").read_text(encoding="utf-8"))
    problems = []
    iot_class = str(manifest.get("iot_class", ""))
    if not iot_class.startswith("local_"):
        problems.append(f"manifest.json: iot_class is {iot_class!r}, not local_*")
    dependencies = set(manifest.get("dependencies", [])) | set(
        manifest.get("after_dependencies", [])
    )
    for dependency in sorted(dependencies & FORBIDDEN_DEPENDENCIES):
        problems.append(f"manifest.json: depends on {dependency!r}")
    for requirement in manifest.get("requirements", []):
        name = re.split(r"[<>=!~\[; @]", requirement, maxsplit=1)[0].lower()
        if name not in {r.lower() for r in ALLOWED_REQUIREMENTS}:
            problems.append(f"manifest.json: requirement {requirement!r} is not allowed")
    return problems


def _check_call(node: ast.Call, path: Path, where: str) -> list[str]:
    name, owner = _call_name(node)
    allowed = _allowed_calls(path)
    keywords = {keyword.arg for keyword in node.keywords if keyword.arg}

    if name in LISTEN_CALLS:
        return [f"{where}: {name}() listens for connections"]
    if name == "create_datagram_endpoint":
        if path.name not in CONNECTED_UDP_FILES or name not in allowed:
            return [f"{where}: create_datagram_endpoint() outside {sorted(CONNECTED_UDP_FILES)}"]
        if "remote_addr" not in keywords or keywords & UDP_LISTEN_ARGUMENTS:
            return [f"{where}: create_datagram_endpoint() is not a connected UDP socket"]
        return []
    if name in CONNECT_CALLS:
        if name in allowed or owner in CLIENT_NAMES:
            return []
        return [f"{where}: {name}() outside {sorted(CONNECTION_FILES)}"]
    if name == "sendto" and (len(node.args) > 1 or "addr" in keywords):
        return [f"{where}: sendto() to an explicit address"]
    if name in SESSION_METHODS and owner == "aiohttp":
        return [f"{where}: aiohttp.{name}() bypasses Home Assistant's session"]
    if name in SESSION_METHODS and "session" in owner.lower():
        if f"session.{name}" in allowed:
            return []
        return [f"{where}: {owner}.{name}() outside {sorted(CONNECTION_FILES)}"]
    if name in SESSION_FACTORIES and path.name not in SESSION_FILES:
        return [f"{where}: {name}() outside {sorted(SESSION_FILES)}"]
    if name in PRIVATE_SESSION_CALLS:
        return [f"{where}: {name}() bypasses Home Assistant's shared session"]
    if name in {"async_call", "call"} and owner == "services":
        return [f"{where}: calls a service of another integration"]
    return []


def _check_text(text: str, where: str) -> list[str]:
    if text in ALLOWED_ADDRESSES:
        return []
    problems = [f"{where}: hard-coded address {match.group(0)!r}" for match in _URL.finditer(text)]
    for pattern in (_IPV4, _IPV6):
        for match in pattern.finditer(text):
            if not _harmless_ip(match.group(0)):
                problems.append(f"{where}: hard-coded IP address {match.group(0)!r}")
    problems += [f"{where}: hard-coded host name {m.group(0)!r}" for m in _HOSTNAME.finditer(text)]
    return problems


def check_source(path: Path, relative: str) -> list[str]:
    """Check one Python file of the integration."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
    problems = []

    for node in ast.walk(tree):
        where = f"{relative}:{getattr(node, 'lineno', '?')}"
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                + [f"{node.module}.{alias.name}" for alias in node.names if node.module]
            )
            for name in names:
                if any(name == m or name.startswith(f"{m}.") for m in FORBIDDEN_MODULES):
                    problems.append(f"{where}: imports {name}")
        elif isinstance(node, ast.Call):
            problems += _check_call(node, path, where)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            problems += _check_text(node.value, where)
    return problems


def find_integration(argv: list[str]) -> Path:
    """Return the integration folder (argument, or the only one in custom_components)."""
    if len(argv) > 1:
        return Path(argv[1])
    root = Path(__file__).resolve().parents[1] / "custom_components"
    folders = [p for p in root.iterdir() if (p / "manifest.json").is_file()]
    if len(folders) != 1:
        raise SystemExit(f"Expected one integration in {root}, found {len(folders)}")
    return folders[0]


def main(argv: list[str]) -> int:
    """Run all checks and report."""
    integration = find_integration(argv)
    problems = check_manifest(integration)
    files = sorted(integration.rglob("*.py"))
    for path in files:
        problems += check_source(path, str(path.relative_to(integration.parent)))

    print(f"Local-only check of {integration.name}: {len(files)} Python files")
    if problems:
        for problem in problems:
            print(f"  FAIL {problem}")
        return 1
    print("  OK   manifest: local IoT class, no cloud, webhook or MQTT, no extra requirements")
    print("  OK   no listeners, webhooks, HTTP views or unconnected UDP sockets")
    for name, calls in sorted(CONNECTION_FILES.items()):
        print(f"  OK   {name}: only {', '.join(sorted(calls))}")
    print("  OK   no private HTTP sessions, fixed UDP targets or service calls")
    print("  OK   no hard-coded internet addresses")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
