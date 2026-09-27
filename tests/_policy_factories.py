"""Crawl-policy defaults, and the one way a test should admit discovered links.

A crawl policy is a handful of sub-mappings that are only ever spelled out
together, and the values in them were re-typed at every site: this file alone
carried the same limits block twice and a third copy of a sibling, and
``admit_discovered_links`` was called with all fourteen of its keywords written
out at each of the three sites that needed it. Each copy is a place a policy
change lands in only one of them, and the copy is the one the test runs.

``_CRAWL_LIMITS`` and ``_STRATEGY_LIMITS`` are the two profiles, named so the
factories below differ only in what is actually different about them, and every
factory hands out a fresh copy because callers are told to mutate the policy
they are given.
"""

from typing import Any

from app.constants import HTTP_DECLARED_CRAWLER
from app.crawl.admission import admit_discovered_links
from app.validation import HostPolicy

UNRESTRICTED_HOST_POLICY = HostPolicy.unrestricted()

BOOL_ENV_CASES = (
    (None, True),
    ("", True),
    ("   ", True),
    ("1", True),
    (" TrUe ", True),
    ("yes", True),
    ("ON", True),
    ("0", False),
    (" FALSE ", False),
    ("No", False),
    ("off", False),
    ("garbage", True),
)

# The minimal-deployment profile: a short per-request timeout and little room
# for discovered links, because a box this small is the one where admitting a
# hundred extra pages is what matters.
_CRAWL_LIMITS = {"timeout_seconds": 5, "max_retries": 0, "max_discovered_links": 10}

# The live-server profile: a generous per-request timeout and room for the
# links a real page actually links to. A fixture that timed out or stopped at
# ten links would report a transport problem for a policy that was too tight.
_STRATEGY_LIMITS = {"timeout_seconds": 10, "max_retries": 0, "max_discovered_links": 50}


def make_base_policy(strategies=None) -> dict:
    return {
        "schema_version": "2026-06-23",
        "strategies": list(strategies) if strategies is not None else [HTTP_DECLARED_CRAWLER],
        "limits": dict(_CRAWL_LIMITS),
        "headers": {},
        "request_profile": {},
        "browser_profile": {},
        "discovery": {},
    }


def _policy(extra=None):
    """The strategy-level policy, with an optional override merged over it.

    Carries no ``strategies`` on purpose: the strategy-level tests want the
    default ladder to climb, and a pinned strategy here would answer a question
    the ladder is what the caller is actually testing.
    """
    policy = {
        "request_profile": {},
        "headers": {},
        "limits": dict(_STRATEGY_LIMITS),
    }
    if extra:
        policy.update(extra)
    return policy


def http_strategy_policy(*, extraction: str = "full", discovery: bool | None = None) -> dict:
    policy: dict[str, Any] = make_base_policy()
    policy["extraction"] = extraction
    if discovery is not None:
        policy["discovery"] = {"links": True} if discovery else {}
    return policy


async def _robots_allow_all(url: str) -> bool:
    return True


def _commit_nothing(domain: str, delay: float) -> None:
    """A no-op crawl-delay commit, so a scenario that never reads it need not pass one."""


async def admit(
    links,
    *,
    frontier,
    max_depth: int = 1,
    max_pages: int = 1,
    source_depth: int = 0,
    known: set[str] | None = None,
    make_entry=lambda link: link.normalized_url,
    scope_pattern=None,
    want_document_bodies: bool = False,
    respect_robots: bool = True,
    robots_allows=None,
    robots_crawl_delay=None,
    commit_crawl_delay=None,
    reserve_admitted: bool = False,
    occupied: int | None = None,
    host_policy: HostPolicy = UNRESTRICTED_HOST_POLICY,
):
    """Run production ``admit_discovered_links`` and return its real outcome.

    Every keyword here mirrors production's, default for default, so the harness
    is the production contract and not a second opinion about it: a new required
    argument in production raises here, once, instead of being missed at one of
    several call sites. There is no ``**kwargs`` catch-all on purpose -- a
    misspelled override must be a ``TypeError`` at the harness rather than an
    argument production never received.

    The three robots keywords default to "nothing blocks, nothing to look up,
    nothing to commit", which is the profile an admission scenario that is not
    about robots wants and should not have to spell. ``frontier`` collects
    ``link.normalized_url`` strings unless a caller supplies its own
    ``make_entry``.
    """
    return await admit_discovered_links(
        links,
        source_depth=source_depth,
        max_depth=max_depth,
        max_pages=max_pages,
        known=set() if known is None else known,
        frontier=frontier,
        make_entry=make_entry,
        scope_pattern=scope_pattern,
        want_document_bodies=want_document_bodies,
        respect_robots=respect_robots,
        robots_allows=_robots_allow_all if robots_allows is None else robots_allows,
        robots_crawl_delay=robots_crawl_delay,
        commit_crawl_delay=_commit_nothing if commit_crawl_delay is None else commit_crawl_delay,
        host_policy=host_policy,
        reserve_admitted=reserve_admitted,
        occupied=occupied,
    )


__all__ = [
    "BOOL_ENV_CASES",
    "UNRESTRICTED_HOST_POLICY",
    "_policy",
    "admit",
    "http_strategy_policy",
    "make_base_policy",
]


def crawl_limits(*, respect_robots: bool | None = None, **over) -> dict:
    """The limits block a crawl fixture needs, with overrides merged over it.

    ``timeout_seconds: 10`` is the live-server profile, on the same reasoning as
    :data:`_STRATEGY_LIMITS` -- a fixture that timed out reports a transport
    problem for a policy that was too tight, which is the most expensive kind of
    wrong. Stated inline it was four copies of the same three numbers, and a
    profile change was four edits of which one would have been missed.

    ``respect_robots`` is a switch tests flip rather than a number they tune, so
    it is a keyword; ``None`` leaves it out of the block entirely rather than
    writing a default no caller asked for.
    """
    limits: dict = {"timeout_seconds": 10, "max_retries": 0, "max_discovered_links": 10}
    if respect_robots is not None:
        limits["respect_robots"] = respect_robots
    limits.update(over)
    return limits


def crawl_policy(strategies=None, *, limits: dict | None = None, discovery: dict | None = None) -> dict:
    """A crawl policy carrying strategies, limits, and the named extra blocks.

    The shape four tests each rebuilt around the same limits block. ``discovery``
    is named rather than passed through ``**extra`` so that what a policy can
    carry is a decision made here instead of a decision each caller makes.
    """
    policy: dict = {
        "strategies": list(strategies) if strategies is not None else [HTTP_DECLARED_CRAWLER],
        "limits": crawl_limits(**(limits or {})),
    }
    if discovery is not None:
        policy["discovery"] = discovery
    return policy
