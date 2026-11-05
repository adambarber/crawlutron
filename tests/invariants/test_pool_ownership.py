"""Cookies enter through the fetch tail."""

from __future__ import annotations

import ast
import pathlib

from tests.chokepoint_walks import REPO_ROOT, offender_at, relative_posix
from tests.invariants._proof_paths import ELSEWHERE as _ELSEWHERE
from tests.invariants._shared import _enclosing_calls
from tests.rules import Bites, Registry, Rule, Silent

# The enforcement half: the jar has exactly one write door, and that door is the shared tail.

_COOKIE_TAIL = "crawlutron/transport/fetchers.py"


_COOKIE_HELPER = "crawlutron/transport/request_profile.py"


# The door's own implementation module: the jar's methods are the write door,
# including its RFC 6265 send matching, which shares the stdlib method name.
_COOKIE_DOOR = "crawlutron/transport/cookie_jar.py"


_COOKIE_TAIL_FN = "_finish_fetch"


# The redirect-hop door: a 302's Set-Cookie persists ONLY through this named helper,
# deferred until the next hop's pin admits the walk (#525); the timing lock is behavioral.
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
            "from crawlutron.transport.request_profile import store_set_cookies\n"
            "def _finish_fetch(**kwargs):\n"
            "    return store_set_cookies(kwargs)\n",
            "store_set_cookies() outside _finish_fetch()",
            at=_ELSEWHERE,
        ),
    ),
    clean=lambda: (
        # The sanctioned two-hop chain, spared at the paths that own it.
        Silent(
            "from crawlutron.transport.request_profile import store_set_cookies\n"
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


RULES = Registry((RESPONSE_COOKIES_ENTER_VIA_THE_FETCH_TAIL,))


def test_no_rule_violations():
    """Cookie persistence has one door, and that door is the shared fetch tail."""
    RULES.check_all_clean()
