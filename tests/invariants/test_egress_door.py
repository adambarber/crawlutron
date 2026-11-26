"""One module owns outbound transport construction: fetchers.py's pooled client.

No other module may build its own httpx/curl_cffi/urlopen client; webhooks.py used
to (webhooks.py:136) and now routes delivery through get_async_http_client().
"""

from __future__ import annotations

import ast

from tests.chokepoint_paths import relative_posix
from tests.chokepoint_walks import call_nodes, offender_at, resolved_call_name
from tests.rules import Bites, Registry, Rule

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


#: The pooled client's dial verbs never take a raw URL string: every verb takes
#: a logical FetchRequest; the proxy owns target DNS and the dial.
_RAW_DIAL_VERBS = frozenset({"get", "post", "stream"})


def _raw_egress_dial_offenders(tree, path):
    """Flag a pooled-client dial verb handed a raw URL string instead of a FetchRequest.

    A string URL there is the raw-dial shape the facade refuses; named file:line.
    """
    offenders = []
    for call in call_nodes(tree):
        if not isinstance(call.func, ast.Attribute) or call.func.attr not in _RAW_DIAL_VERBS:
            continue
        args = call.args
        url_index = 1 if call.func.attr == "stream" else 0
        if len(args) <= url_index:
            continue
        url = args[url_index]
        if (
            isinstance(url, ast.Constant)
            and isinstance(url.value, str)
            and url.value.startswith(("http://", "https://"))
        ):
            offenders.append(offender_at(path, call, "pooled-client dial of a raw URL string"))
    return offenders


RAW_EGRESS_DIAL = Rule(
    name="raw-egress-dial-through-pooled-client",
    find=_raw_egress_dial_offenders,
    summary=(
        "get_async_http_client() hands out a facade whose get/post/stream take a "
        "FetchRequest, never a raw URL string; dialling a caller-supplied host at "
        "OS resolution time is inexpressible through this surface"
    ),
    bite=lambda: Bites(
        'client.get("http://example.test/")',
        marker="pooled-client dial of a raw URL string",
        count=1,
    ),
    clean=lambda: (
        # The sanctioned shape: a FetchRequest, never a raw URL string.
        "client.stream('GET', pinned, follow_redirects=False)\n",
    ),
)


RULES = Registry((INDEPENDENT_EGRESS_DOOR, RAW_EGRESS_DIAL))


def test_independent_egress_door():
    """No module but fetchers.py's pool home constructs an outbound client."""
    RULES.check_all_clean()


def test_raw_egress_dial_is_named_by_file_and_line():
    """The pooled client's dial verbs refuse a raw URL string, and a planted
    client.get("http://example.test/") is named by file:line."""
    RULES.check_all_bite()


def test_egress_proxy_docstring_declares_the_sole_door() -> None:
    """The shipped topology makes this the only egress door; no stale shadow claim."""
    from crawlutron.transport import egress_proxy

    doc = egress_proxy.__doc__ or ""
    assert "Shadow mode" not in doc, "stale shadow-mode claim in the egress door"
    assert "live egress paths are untouched" not in doc, "stale untouched-paths claim in the egress door"
    assert "sole" in doc.lower() and "egress" in doc.lower(), "docstring must declare the sole egress door"
