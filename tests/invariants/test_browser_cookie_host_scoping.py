"""Harvested cookies are keyed by exact host, never the registrable site.

The gate drives the production harvest/replay door and pins the source keying:
a planted ``url.site`` key reds with the offending file and line.
"""

import ast
from types import SimpleNamespace

from crawlutron.runtime import process_state
from crawlutron.transport.browser_cookies import get_browser_cookies_for, harvest_session_cookies
from tests._policy_factories import canonical
from tests.chokepoint_paths import APP_ROOT
from tests.chokepoint_walks import canonical_tree, nodes_of, offender_at

_HARVEST_HOME = APP_ROOT / "transport" / "browser_cookies.py"
_CHAIN_HOME = APP_ROOT / "orchestration" / "strategy_chain.py"
_KEY_MSG = "harvested cookie keyed by the registrable site instead of the exact host"
_KEY_CALLS = frozenset({"store_browser_cookies", "get_browser_cookies_for"})


def _uses_site(node: ast.AST) -> bool:
    return any(isinstance(part, ast.Attribute) and part.attr == "site" for part in ast.walk(node))


def _site_key_offenders(tree: ast.Module, path) -> list[str]:
    out: list[str] = []
    for node in nodes_of(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func
        if not (isinstance(callee, ast.Name) and callee.id in _KEY_CALLS):
            continue
        if node.args and _uses_site(node.args[0]):
            out.append(offender_at(path, node, _KEY_MSG))
    return out


def test_no_site_keyed_cookie_calls():
    """Neither the harvest nor the replay door keys the store by the site."""
    assert not _site_key_offenders(canonical_tree(_HARVEST_HOME), _HARVEST_HOME)
    assert not _site_key_offenders(canonical_tree(_CHAIN_HOME), _CHAIN_HOME)


def test_planted_site_key_names_the_offender_file_and_line():
    """The gate bites a planted site key, naming file:line for the reviewer."""
    source = "def f(url):\n    store_browser_cookies(url.site, header, namespace=ns)\n"
    offenders = _site_key_offenders(ast.parse(source), _HARVEST_HOME)
    assert len(offenders) == 1, offenders
    assert _KEY_MSG in offenders[0], offenders
    assert "browser_cookies.py:2 " in offenders[0], offenders


async def test_gate_drives_the_production_harvest_and_replay_door():
    """Two subdomains and a Secure cookie must not cross through the real doors."""

    class _Context:
        async def cookies(self, url):
            return [{"name": "sid", "value": "A", "secure": True}]

    url = canonical("https://a.tenant.example/login")
    process_state.reset_all_process_state()
    try:
        await harvest_session_cookies(SimpleNamespace(context=_Context()), url)
        leaked = get_browser_cookies_for("b.tenant.example", scheme="https", path="/app")
        assert leaked is None, f"site-keyed replay leaked A's session to B: {leaked}"
        assert get_browser_cookies_for(url.host, scheme="http", path="/login") is None
        assert get_browser_cookies_for(url.host, scheme="https", path="/login") == "sid=A"
    finally:
        process_state.reset_all_process_state()
