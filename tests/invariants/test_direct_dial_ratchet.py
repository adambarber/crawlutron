"""Direct egress dials are an admitted-surface ratchet: new dials cannot land.

The proxy owns its pinned dial; every other site is admitted and moves down only.
"""

from __future__ import annotations

import ast
import collections

import pytest

from tests._walk_primitives import offender_at, parsed_source
from tests._walk_resolution import resolved_call_name, resolved_expr_name
from tests.chokepoint_paths import REPO_ROOT, relative_posix
from tests.chokepoint_walks import app_trees, call_nodes
from tests.rules import Bites, Rule

_SANCTIONED = frozenset(
    {
        # The proxy dials the pinned IP; its dial is the door, not a bypass.
        "packages/core/src/crawlutron/transport/egress_proxy.py",
        # Inbound-to-service SDK/CLI clients do not make the service's egress.
        "packages/rest/src/crawlutron_rest/cli/client.py",
    }
)

#: Raw public dials: socket construction/creation and the connect method.
_RAW_SOCKET_NAMES = frozenset({"socket.socket", "socket.create_connection"})

#: asyncio open_connection is a raw public dial.
_ASYNCIO_OPEN_CONNECTION = frozenset({"asyncio.open_connection"})

#: urllib.request.urlopen is a raw public dial.
_URLLIB_URLOPEN = frozenset({"urllib.request.urlopen"})

#: Outbound client/session construction that must carry proxy= or proxies= to be sanctioned.
_CLIENT_NAMES = frozenset(
    {
        "httpx.AsyncClient",
        "httpx.Client",
        "curl_cffi.requests.AsyncSession",
        "cffi_requests.AsyncSession",
    }
)

#: Module-level httpx helpers that dial at call time (never sanctioned inline).
_HELPER_NAMES = frozenset(
    {
        "httpx.get",
        "httpx.post",
        "httpx.put",
        "httpx.delete",
        "httpx.head",
        "httpx.options",
        "httpx.patch",
        "httpx.stream",
        "httpx.request",
    }
)

#: Outbound libraries whose calls are always raw egress (no sanctioned proxy shape).
_REQUESTS_PREFIX = "requests."
_AIOHTTP_PREFIX = "aiohttp."

_MARKER = "direct egress dial outside the proxy door"


def _has_proxy_keyword(call) -> bool:
    """True when ``call`` passes ``proxy=`` or ``proxies=`` as a keyword argument."""
    return any(keyword.arg in ("proxy", "proxies") for keyword in call.keywords)


def _is_socket_connect(call, tree) -> bool:
    """True when ``call`` is a ``.connect()`` method call on a socket-valued receiver."""
    if not isinstance(call.func, ast.Attribute) or call.func.attr != "connect":
        return False
    receiver = resolved_expr_name(tree, call.func.value)
    return receiver == "socket.socket" or receiver.startswith("socket.") or receiver == "socket"


def _direct_dial_offenders(tree, path) -> list[str]:
    """Flag raw socket bypass and unproxied client construction outside the proxy door."""
    if relative_posix(path) in _SANCTIONED:
        return []
    offenders = []
    for node in call_nodes(tree):
        name = resolved_call_name(tree, node)
        if name in _RAW_SOCKET_NAMES:
            offenders.append(offender_at(path, node, _MARKER))
        if _is_socket_connect(node, tree):
            offenders.append(offender_at(path, node, _MARKER))
        if name in _ASYNCIO_OPEN_CONNECTION:
            offenders.append(offender_at(path, node, _MARKER))
        if name in _URLLIB_URLOPEN:
            offenders.append(offender_at(path, node, _MARKER))
        if name in _HELPER_NAMES:
            offenders.append(offender_at(path, node, _MARKER))
        if name.startswith(_REQUESTS_PREFIX) or name.startswith(_AIOHTTP_PREFIX):
            offenders.append(offender_at(path, node, _MARKER))
        if name in _CLIENT_NAMES and not _has_proxy_keyword(node):
            offenders.append(offender_at(path, node, _MARKER))
    return offenders


DIRECT_DIAL = Rule(
    name="direct-dial-ratchet",
    find=_direct_dial_offenders,
    bite=lambda: Bites("client = httpx.AsyncClient()\n", marker=_MARKER),
    summary="new outbound dial sites must be routed through the loopback proxy door",
    clean=lambda: ("value = mapping.get('key')\n",),
)

# #1364 S5 topology: ratchet collapses to zero; raw sockets/unproxied clients refused.
_RATCHET = {"search": 0, "service": 0, "transport": 0}


def _group(relative: str) -> str:
    """Derive the admission group from path structure, never a hand-listed row."""
    anchor = "crawlutron/"
    if anchor in relative:
        return relative.split(anchor, 1)[1].split("/", 1)[0]
    return relative.split("/", 1)[0]


def _counts() -> collections.Counter[str]:
    counts: collections.Counter[str] = collections.Counter()
    for path, tree in app_trees():
        relative = relative_posix(path)
        if relative in _SANCTIONED:
            continue
        counts[_group(relative)] += len(_direct_dial_offenders(tree, path))
    return counts


def _row_offenders(row: str) -> list[str]:
    return [
        offender
        for path, tree in app_trees()
        if relative_posix(path) not in _SANCTIONED and _group(relative_posix(path)) == row
        for offender in _direct_dial_offenders(tree, path)
    ]


@pytest.mark.parametrize("row", sorted(_RATCHET))
def test_direct_dials_never_exceed_the_ratchet(row: str) -> None:
    """A new direct dial must be paid for by deleting an admitted one."""
    found = _counts()[row]
    assert found <= _RATCHET[row], f"{row}: {found} direct dials exceed {_RATCHET[row]}:\n" + "\n  ".join(
        _row_offenders(row)
    )


@pytest.mark.parametrize("row", sorted(_RATCHET))
def test_the_ratchet_is_tight(row: str) -> None:
    """A migrated dial leaves a stale admission the same commit must remove."""
    found = _counts()[row]
    assert _RATCHET[row] <= found, f"{row}: ratchet {_RATCHET[row]} is stale; tree has {found}"


def test_the_gate_walks_every_app_tree() -> None:
    """The derivation covers every production module; the bite is proven by the
    Rule/Bites test, not by production offenders (topology drives counts to zero)."""
    assert len(app_trees()) > 100


def test_detector_bites_and_spares_lookalikes() -> None:
    """Rule/Bites proof: a direct dial fires by file:line, a dict get stays quiet."""
    DIRECT_DIAL.check_bites()


def test_exemption_is_by_relative_path_not_basename() -> None:
    """A decoy named egress_proxy.py still fires; only the exact home is quiet."""
    source = "client = httpx.AsyncClient()\n"
    decoy = REPO_ROOT / "tests" / "decoys" / "egress_proxy.py"
    assert _direct_dial_offenders(parsed_source(source, filename=str(decoy)), decoy) == [
        "tests/decoys/egress_proxy.py:1 " + _MARKER
    ]
    home = REPO_ROOT / "packages" / "core" / "src" / "crawlutron" / "transport" / "egress_proxy.py"
    assert _direct_dial_offenders(parsed_source(source, filename=str(home)), home) == []


def test_raw_socket_ops_fire_outside_proxy_home() -> None:
    """socket.socket, socket.create_connection and .connect() are raw public dials."""
    assert _direct_dial_offenders(
        parsed_source("s = socket.socket()\n", filename="x/socket_client.py"),
        REPO_ROOT / "x" / "socket_client.py",
    ) == ["x/socket_client.py:1 " + _MARKER]
    assert _direct_dial_offenders(
        parsed_source("socket.create_connection(('example.com', 443))\n", filename="x/cc.py"),
        REPO_ROOT / "x" / "cc.py",
    ) == ["x/cc.py:1 " + _MARKER]
    # socket.socket().connect() fires twice: socket creation + connect, both raw dials.
    assert _direct_dial_offenders(
        parsed_source("socket.socket().connect(('example.com', 443))\n", filename="x/conn.py"),
        REPO_ROOT / "x" / "conn.py",
    ) == [
        "x/conn.py:1 " + _MARKER,
        "x/conn.py:1 " + _MARKER,
    ]


def test_asyncio_open_connection_fires_outside_proxy_home() -> None:
    """asyncio.open_connection is a raw public dial."""
    assert _direct_dial_offenders(
        parsed_source("await asyncio.open_connection('example.com', 443)\n", filename="x/asyncio_conn.py"),
        REPO_ROOT / "x" / "asyncio_conn.py",
    ) == ["x/asyncio_conn.py:1 " + _MARKER]


def test_urllib_request_urlopen_fires_outside_proxy_home() -> None:
    """urllib.request.urlopen is a raw public dial."""
    assert _direct_dial_offenders(
        parsed_source("urllib.request.urlopen('http://example.com/')\n", filename="x/urlopen.py"),
        REPO_ROOT / "x" / "urlopen.py",
    ) == ["x/urlopen.py:1 " + _MARKER]


def test_unproxied_client_construction_fires() -> None:
    """httpx.AsyncClient / curl_cffi AsyncSession without proxy= are refused."""
    assert _direct_dial_offenders(
        parsed_source("client = httpx.AsyncClient()\n", filename="x/unproxied.py"),
        REPO_ROOT / "x" / "unproxied.py",
    ) == ["x/unproxied.py:1 " + _MARKER]
    assert _direct_dial_offenders(
        parsed_source("s = cffi_requests.AsyncSession()\n", filename="x/cffi.py"),
        REPO_ROOT / "x" / "cffi.py",
    ) == ["x/cffi.py:1 " + _MARKER]


def test_proxied_client_construction_stays_clean() -> None:
    """proxy= or proxies= construction is sanctioned topology, not a bypass."""
    # proxy= singular
    proxied = "client = httpx.AsyncClient(proxy=egress_proxy_url(HostPolicy.from_settings(s)))\n"
    assert (
        _direct_dial_offenders(
            parsed_source(proxied, filename="x/proxied.py"), REPO_ROOT / "x" / "proxied.py"
        )
        == []
    )
    # proxies= plural
    proxies = "client = httpx.AsyncClient(proxies={'http://': 'http://proxy:8080'})\n"
    assert (
        _direct_dial_offenders(
            parsed_source(proxies, filename="x/proxies.py"), REPO_ROOT / "x" / "proxies.py"
        )
        == []
    )
    cffi_proxied = "s = cffi_requests.AsyncSession(proxy=egress_proxy_url(policy))\n"
    assert (
        _direct_dial_offenders(
            parsed_source(cffi_proxied, filename="x/cffi_proxied.py"), REPO_ROOT / "x" / "cffi_proxied.py"
        )
        == []
    )


def test_module_level_httpx_helpers_fire() -> None:
    """httpx.get / httpx.post / httpx.stream dial at call time and are refused."""
    for helper in ("httpx.get", "httpx.post", "httpx.stream", "httpx.request"):
        source = f"{helper}('http://example.test/')\n"
        assert _direct_dial_offenders(
            parsed_source(source, filename="x/helper.py"), REPO_ROOT / "x" / "helper.py"
        ) == [f"x/helper.py:1 {_MARKER}"], helper


def test_requests_and_aiohttp_calls_fire() -> None:
    """requests.* and aiohttp.* outbound calls have no sanctioned proxy shape."""
    for prefix, call in (
        ("requests.", "requests.get('http://example.test/')"),
        ("aiohttp.", "aiohttp.ClientSession()"),
    ):
        source = call + "\n"
        assert _direct_dial_offenders(
            parsed_source(source, filename="x/lib.py"), REPO_ROOT / "x" / "lib.py"
        ) == [f"x/lib.py:1 {_MARKER}"], prefix


def test_decoy_named_egress_proxy_still_fires_for_socket_ops() -> None:
    """A decoy named egress_proxy.py still fires; only the exact home is quiet."""
    decoy = REPO_ROOT / "tests" / "decoys" / "egress_proxy.py"
    assert _direct_dial_offenders(parsed_source("socket.socket()\n", filename=str(decoy)), decoy) == [
        "tests/decoys/egress_proxy.py:1 " + _MARKER
    ]
    home = REPO_ROOT / "packages" / "core" / "src" / "crawlutron" / "transport" / "egress_proxy.py"
    assert _direct_dial_offenders(parsed_source("socket.socket()\n", filename=str(home)), home) == []
