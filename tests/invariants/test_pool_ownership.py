"""Pool slots are reaped; cookies enter through the fetch tail."""

from __future__ import annotations

import ast
import pathlib

from tests.chokepoint_walks import REPO_ROOT, nodes_of, offender_at, relative_posix, unary_ops
from tests.invariants._shared import _enclosing_calls
from tests.invariants.test_ownership import _ELSEWHERE
from tests.rules import Bites, Registry, Rule, Silent

_DOC_POOL_CLASS = "DocExtractionPool"


_DOC_POOL_ACQUIRER = "acquire"


def _has_retired_attr(node: ast.AST) -> bool:
    return any(isinstance(sub, ast.Attribute) and sub.attr == "retired" for sub in nodes_of(node))


def _reaps_retired(value: ast.AST) -> bool:
    """A ``self._slots = [... for each... if not each.retired]`` style rebuild.

    ``not each.retired`` parses as a ``UnaryOp(Not,...)`` and an explicit
    comparison as a ``Compare``; both shapes are admitted.
    """
    if not isinstance(value, ast.ListComp):
        return False
    for generator in value.generators:
        for condition in generator.ifs:
            for node in unary_ops(condition):
                if isinstance(node.op, ast.Not):
                    if _has_retired_attr(node.operand):
                        return True
                if isinstance(node, ast.Compare):
                    if any(_has_retired_attr(operand) for operand in (node.left, *node.comparators)):
                        return True
    return False


def _rebuilds_pool_without_retired(node: ast.AST) -> bool:
    """Does ``node`` assign ``self._slots`` from a list comp filtering ``retired``?"""
    return any(
        isinstance(child, ast.Assign)
        and any(isinstance(target, ast.Attribute) and target.attr == "_slots" for target in child.targets)
        and _reaps_retired(child.value)
        for child in nodes_of(node)
    )


def _doc_pool_reap_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag ``acquire`` that never rebuilds the pool without retired slots."""
    return [
        offender_at(
            path,
            node,
            f"{_DOC_POOL_CLASS}.{_DOC_POOL_ACQUIRER} no longer reaps retired slots; a "
            f"retired-but-alive slot would strand in the pool and inflate the size budget",
        )
        for cls in (each for each in nodes_of(tree) if isinstance(each, ast.ClassDef))
        if cls.name == _DOC_POOL_CLASS
        for node in cls.body
        if isinstance(node, ast.FunctionDef)
        and node.name == _DOC_POOL_ACQUIRER
        and not _rebuilds_pool_without_retired(node)
    ]


DOC_POOL_ACQUIRER_REAPS_RETIRED_SLOTS = Rule(
    name="doc-pool-acquirer-reaps-retired-slots",
    find=_doc_pool_reap_offenders,
    summary=(
        "A retired slot can never be handed out, so leaving one in the pool "
        "strands it forever and inflates the pool-size budget; rebuild the pool "
        "without retired entries in DocExtractionPool.acquire"
    ),
    bite=lambda: (
        Bites(
            "class DocExtractionPool:\n"
            "    def acquire(self):\n"
            "        with self._lock:\n"
            "            want = self._worker_count()\n"
            "            while True:\n"
            "                for worker in self._slots:\n"
            "                    if not worker.busy:\n"
            "                        return worker\n",
            "no longer reaps retired slots",
        ),
        Bites(
            "class DocExtractionPool:\n"
            "    def acquire(self):\n"
            "        with self._lock:\n"
            "            self._slots = [each for each in self._slots if not each.busy]\n"
            "            return self._slots[0]\n",
            "no longer reaps retired slots",
        ),
    ),
    clean=lambda: (
        "class DocExtractionPool:\n"
        "    def acquire(self):\n"
        "        with self._lock:\n"
        "            self._slots = [each for each in self._slots if not each.retired]\n"
        "            return self._slots[0]\n",
    ),
)


# These two rules are the enforcement half: the jar has exactly one write door, and that door is the shared
# tail.

_COOKIE_TAIL = "app/transport/fetchers.py"


_COOKIE_HELPER = "app/transport/request_profile.py"


# The door's own implementation module: the jar's methods are the write door,
# including its RFC 6265 send matching, which shares the stdlib method name.
_COOKIE_DOOR = "app/transport/cookie_jar.py"


_COOKIE_TAIL_FN = "_finish_fetch"


# The redirect-hop door: a 302's Set-Cookie must persist before the walk
# advances, so the two redirect loops persist through this named helper.
_COOKIE_TAIL_HOP_FN = "_store_hop_cookies"


_COOKIE_TAIL_FNS = frozenset((_COOKIE_TAIL_FN, _COOKIE_TAIL_HOP_FN))


_COOKIE_HELPER_FN = "store_set_cookies"


def _cookie_writes_off_the_fetch_tail(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag a cookie write that does not run through the shared fetch tail."""
    relative = relative_posix(path)
    offenders: list[str] = []
    for call, enclosing in _enclosing_calls(tree, "store_set_cookies"):
        if relative == _COOKIE_TAIL and enclosing in _COOKIE_TAIL_FNS:
            continue
        offenders.append(offender_at(path, call, f"store_set_cookies() outside {_COOKIE_TAIL_FN}()"))
    for call, enclosing in _enclosing_calls(tree, "add_cookie_header"):
        if relative == _COOKIE_DOOR:
            continue  # the jar's own implementation, send matching included
        if relative == _COOKIE_HELPER and enclosing == _COOKIE_HELPER_FN:
            continue
        offenders.append(offender_at(path, call, f"add_cookie_header() outside {_COOKIE_HELPER_FN}()"))
    return offenders


RESPONSE_COOKIES_ENTER_VIA_THE_FETCH_TAIL = Rule(
    name="response-cookies-enter-via-the-fetch-tail",
    find=_cookie_writes_off_the_fetch_tail,
    summary=(
        "The httpx and curl_cffi fetchers each carried their own cookie tail and "
        "had already drifted, so a third fetch path growing its own handling is a "
        "silently missing cookie; persist through fetchers._finish_fetch"
    ),
    bite=lambda: (
        Bites(
            "import x\nx.store_set_cookies(url, headers)\n",
            "store_set_cookies() outside _finish_fetch()",
        ),
        Bites(
            "import jar\njar.add_cookie_header(url, cookie)\n",
            "add_cookie_header() outside store_set_cookies()",
        ),
        Bites("def nested():\n    jar.add_cookie_header(url, cookie)\n", "add_cookie_header() outside"),
        # The half that makes it a PATH-keyed rule: that very same fetch-tail source still fires anywhere
        # else.
        Bites(
            "from app.transport.request_profile import store_set_cookies\n"
            "def _finish_fetch(**kwargs):\n"
            "    return store_set_cookies(kwargs)\n",
            "store_set_cookies() outside _finish_fetch()",
            at=_ELSEWHERE,
        ),
    ),
    clean=lambda: (
        # The sanctioned two-hop chain, spared at the paths that own it.
        Silent(
            "from app.transport.request_profile import store_set_cookies\n"
            "def _finish_fetch(**kwargs):\n"
            "    return store_set_cookies(kwargs)\n",
            at=REPO_ROOT / _COOKIE_TAIL,
        ),
        Silent(
            "def store_set_cookies(url, headers):\n    jar.add_cookie_header(url, c)\n",
            at=REPO_ROOT / _COOKIE_HELPER,
        ),
    ),
    proof_path=REPO_ROOT / _COOKIE_TAIL,
)


RULES = Registry((DOC_POOL_ACQUIRER_REAPS_RETIRED_SLOTS, RESPONSE_COOKIES_ENTER_VIA_THE_FETCH_TAIL))


def test_no_rule_violations():
    """Cookie persistence has one door, and that door is the shared fetch tail."""
    RULES.check_all_clean()
