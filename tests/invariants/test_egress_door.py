"""One module owns outbound transport construction: fetchers.py's pooled client.

No other module may build its own httpx/curl_cffi/urlopen client; webhooks.py used
to (webhooks.py:136) and now routes delivery through get_async_http_client().
"""

from __future__ import annotations

from tests.chokepoint_walks import call_nodes, offender_at, relative_posix, resolved_call_name
from tests.rules import Bites, Rule

#: The sanctioned egress home: fetchers.py owns the pooled client; nothing else builds one.
_EGRESS_HOME = "packages/core/src/crawlutron/transport/fetchers.py"

#: Modules that legitimately construct the pooled client/session themselves (the egress
#: machinery), and therefore are out of scope for this rule.
_SANCTIONED_CONSTRUCTION = frozenset(
    {
        "packages/core/src/crawlutron/transport/pooled_clients.py",
        "packages/core/src/crawlutron/transport/pooled_cffi.py",
    }
)

#: Inbound-to-service consumers (SDK/CLI clients) are allowlisted explicitly: they build
#: their own clients to talk TO the service, not to make the service's outbound egress.
_ALLOWLISTED_CONSUMERS = frozenset(
    {
        "packages/sdk/src/crawlutron_sdk/client.py",
        "packages/rest/src/crawlutron_rest/cli/client.py",
    }
)


def _is_independent_construction(tree, call) -> bool:
    """True when ``call`` builds an independent outbound client/session."""
    callee = resolved_call_name(tree, call)
    leaf = callee.split(".")[-1]
    if callee.startswith("httpx.") and leaf in {"AsyncClient", "Client"}:
        return True
    if callee.startswith("curl_cffi.") and leaf == "AsyncSession":
        return True
    if leaf == "urlopen":
        return True
    return False


def _independent_egress_offenders(tree, path) -> list[str]:
    """Flag every independent client construction outside the sanctioned egress home."""
    relative = relative_posix(path)
    if relative in _SANCTIONED_CONSTRUCTION or relative in _ALLOWLISTED_CONSUMERS:
        return []
    offenders = []
    for call in call_nodes(tree):
        if _is_independent_construction(tree, call):
            offenders.append(
                offender_at(
                    path,
                    call,
                    "independent egress client outside fetchers.py's pooled client (get_async_http_client)",
                )
            )
    return offenders


INDEPENDENT_EGRESS_DOOR = Rule(
    name="independent-egress-door",
    find=_independent_egress_offenders,
    summary=(
        "webhooks.py built its own httpx.AsyncClient (webhooks.py:136); outbound transport "
        f"construction belongs to {_EGRESS_HOME}'s pooled client, so no other module may "
        "construct httpx.AsyncClient/Client, curl_cffi AsyncSession, or urlopen"
    ),
    bite=lambda: (
        Bites(
            "import httpx\n"
            "async def deliver():\n"
            "    client = httpx.AsyncClient(timeout=httpx.Timeout(5.0))\n",
            "independent egress client outside fetchers.py's pooled client",
        ),
    ),
    clean=lambda: (
        # The sanctioned pooled-client consumer shape must stay quiet.
        "from crawlutron.transport.http_client_pool import get_async_http_client\n"
        "client = get_async_http_client()\n",
    ),
)


def test_independent_egress_door():
    """No module but fetchers.py's pool home constructs an outbound client."""
    INDEPENDENT_EGRESS_DOOR.scan()
