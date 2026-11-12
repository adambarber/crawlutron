"""The browser-cookie harvest is read only through the named accessors.

A caller's harvested session lives under its own namespace in the store
global; a direct read anywhere else routes one caller's jar into another."""

from __future__ import annotations

import ast
import pathlib
from functools import cache

from tests.chokepoint_walks import REPO_ROOT, app_trees, nodes_of, offender_at, relative_posix
from tests.invariants._proof_paths import ELSEWHERE as _ELSEWHERE
from tests.invariants._shared import _enclosing_defs, module_level_bindings
from tests.rules import Bites, Registry, Rule, Silent

# The store's only sanctioned readers: the two named accessors, the lifecycle
# helpers, and the internal implementation. Everything else is a leak.
_STORE_NAME = "BROWSER_COOKIES_BY_DOMAIN"
_STORE_CLASS = "_LockedDomainCookieStore"
_SANCTIONED_FUNCTIONS = frozenset({"store_browser_cookies", "get_browser_cookies_for", "_reset", "_has"})
_READ_MSG = f"reads {_STORE_NAME} outside the sanctioned accessors"


@cache
def _owner_relative() -> str:
    """The module binding the store, located by name through the derived app walk."""
    for path, tree in app_trees():
        if _STORE_NAME in module_level_bindings(tree):
            return relative_posix(path)
    raise AssertionError(f"no app module binds {_STORE_NAME} at module scope")


@cache
def _owner_path() -> pathlib.Path:
    """``REPO_ROOT / _owner_relative()``, the proof site for a sanctioned read."""
    return REPO_ROOT / _owner_relative()


def _read_of_store(node: ast.AST) -> bool:
    """Whether ``node`` is a Load reference to the store (bare name or qualified)."""
    if isinstance(node, ast.Name):
        return node.id == _STORE_NAME and isinstance(node.ctx, ast.Load)
    if isinstance(node, ast.Attribute):
        return node.attr == _STORE_NAME and isinstance(node.ctx, ast.Load)
    return False


def _flag_store_reads(
    tree: ast.Module,
    path: pathlib.Path,
    *,
    sanctioned_functions: frozenset[str],
    sanctioned_class: str,
) -> list[str]:
    """Walk ``BROWSER_COOKIES_BY_DOMAIN`` reads; parameterized for the exemption proofs."""
    home = _owner_relative()
    offenders: list[str] = []
    for node in nodes_of(tree):
        if not _read_of_store(node):
            continue
        if relative_posix(path) == home:
            scopes = _enclosing_defs(node, tree)
            if any(name in sanctioned_functions for name in scopes) or sanctioned_class in scopes:
                continue
        offenders.append(offender_at(path, node, _READ_MSG))
    return offenders


def _probe_owner(
    source: str,
    *,
    sanctioned_functions: frozenset[str] = _SANCTIONED_FUNCTIONS,
    sanctioned_class: str = _STORE_CLASS,
) -> list[str]:
    """The offenders ``source`` produces when read as the store's home module."""
    return _flag_store_reads(
        ast.parse(source),
        _owner_path(),
        sanctioned_functions=sanctioned_functions,
        sanctioned_class=sanctioned_class,
    )


def _browser_cookie_read_offenders(tree: ast.Module, path: pathlib.Path) -> list[str]:
    """Flag any harvest read not running through the store's sanctioned readers."""
    return _flag_store_reads(
        tree,
        path,
        sanctioned_functions=_SANCTIONED_FUNCTIONS,
        sanctioned_class=_STORE_CLASS,
    )


BROWSER_COOKIES_READ_ONLY_THROUGH_THE_ACCESSORS = Rule(
    name="browser-cookies-read-only-through-the-accessors",
    find=_browser_cookie_read_offenders,
    summary=(
        "A harvest read outside store_browser_cookies/get_browser_cookies_for and "
        "the store's own lifecycle/implementation surfaces one caller's session "
        "into another's jar; the store global is consumed only in its home module"
    ),
    bite=lambda: (
        Bites(
            "def steal(domain):\n    return browser_cookies.BROWSER_COOKIES_BY_DOMAIN.touch_get(domain)\n",
            _READ_MSG,
        ),
        Bites(
            "from crawlutron.transport.browser_cookies import BROWSER_COOKIES_BY_DOMAIN\n"
            "def steal(domain):\n"
            "    BROWSER_COOKIES_BY_DOMAIN.touch_get(domain)\n",
            _READ_MSG,
            at=_ELSEWHERE,
        ),
    ),
    clean=lambda: (
        # A sanctioned accessor read, quiet in the module that owns the store.
        Silent(
            "def get_browser_cookies_for(domain, *, namespace=None):\n"
            "    return BROWSER_COOKIES_BY_DOMAIN.touch_get(domain, namespace)\n",
            at=_owner_path(),
        ),
        # The store's own definition is a write, never a read.
        "BROWSER_COOKIES_BY_DOMAIN = _LockedDomainCookieStore(500)\n",
    ),
)

RULES = Registry((BROWSER_COOKIES_READ_ONLY_THROUGH_THE_ACCESSORS,))


def test_no_rule_violations():
    """The whole production tree reads the harvest only through the accessors."""
    RULES.check_all_clean()


def test_browser_cookies_rules_bite():
    """Each injected off-accessor read fires with its named message."""
    RULES.check_all_bite()


def test_removing_the_class_exemption_reds_a_class_read():
    """Killing the _LockedDomainCookieStore exemption re-opens the store's own method."""
    source = (
        "class _LockedDomainCookieStore:\n"
        "    def touch_get(self, domain, namespace=''):\n"
        "        return BROWSER_COOKIES_BY_DOMAIN\n"
    )
    assert _probe_owner(source) == []
    killed = _probe_owner(source, sanctioned_class="_KILLED_CLASS")
    assert killed, "removing the class exemption left the class read unguarded"


def test_narrowing_the_sanctioned_set_to_one_accessor_reds_the_other():
    """Dropping one named accessor re-opens its sanctioned read."""
    source = (
        "def store_browser_cookies(domain, header, *, namespace=None):\n"
        "    BROWSER_COOKIES_BY_DOMAIN.store(domain, header, namespace)\n"
        "\n"
        "\n"
        "def get_browser_cookies_for(domain, *, namespace=None):\n"
        "    return BROWSER_COOKIES_BY_DOMAIN.touch_get(domain, namespace)\n"
    )
    assert _probe_owner(source) == []
    only_store = _probe_owner(source, sanctioned_functions=frozenset({"store_browser_cookies"}))
    assert [hit for hit in only_store if ":6 " in hit], (
        f"dropping get_browser_cookies_for did not red its read: {only_store}"
    )
    only_get = _probe_owner(source, sanctioned_functions=frozenset({"get_browser_cookies_for"}))
    assert [hit for hit in only_get if ":2 " in hit], (
        f"dropping store_browser_cookies did not red its read: {only_get}"
    )


def test_removing_the_lifecycle_helper_exemption_reds_their_reads():
    """Deleting the _reset/_has exemption re-opens the store's lifecycle reads."""
    source = (
        "def _reset():\n"
        "    BROWSER_COOKIES_BY_DOMAIN.clear()\n"
        "\n"
        "\n"
        "def _has():\n"
        "    return len(BROWSER_COOKIES_BY_DOMAIN) > 0\n"
    )
    assert _probe_owner(source) == []
    no_lifecycle = _probe_owner(source, sanctioned_functions=_SANCTIONED_FUNCTIONS - {"_reset", "_has"})
    assert len(no_lifecycle) == 2, (
        f"dropping the lifecycle helpers left their reads unguarded: {no_lifecycle}"
    )
