"""Direct egress dials are an admitted-surface ratchet: new dials cannot land.

The proxy owns its pinned dial; every other site is admitted and moves down only.
"""

from __future__ import annotations

import collections

import pytest

from tests._walk_primitives import offender_at, parsed_source
from tests._walk_resolution import resolved_call_name
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

_DIRECT_NAMES = frozenset(
    {
        "httpx.AsyncClient",
        "httpx.Client",
        "curl_cffi.requests.AsyncSession",
        "cffi_requests.AsyncSession",
        "asyncio.open_connection",
        "socket.create_connection",
        "urllib.request.urlopen",
    }
)
_DIRECT_SUFFIXES = (".get_async_http_client", ".get_cffi_session", ".build_pinned_cffi_session", ".goto")
_MARKER = "direct egress dial outside the proxy door"


def _direct_dial_offenders(tree, path) -> list[str]:
    """Flag every direct-dial call in one module, named by file and line."""
    if relative_posix(path) in _SANCTIONED:
        return []
    return [
        offender_at(path, node, _MARKER)
        for node in call_nodes(tree)
        if (resolved_call_name(tree, node) in _DIRECT_NAMES)
        or resolved_call_name(tree, node).endswith(_DIRECT_SUFFIXES)
    ]


DIRECT_DIAL = Rule(
    name="direct-dial-ratchet",
    find=_direct_dial_offenders,
    bite=lambda: Bites("client = httpx.AsyncClient()\n", marker=_MARKER),
    summary="new outbound dial sites must be routed through the loopback proxy door",
    clean=lambda: ("value = mapping.get('key')\n",),
)

_RATCHET = {"search": 1, "service": 1, "transport": 8}


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
    """The derivation covers every production module and bites real code."""
    assert len(app_trees()) > 100
    assert sum(_counts().values()) > 0


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
