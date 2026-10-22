"""Crawl-policy defaults, and the one way a test should admit discovered links.

The policy sub-mappings were re-typed at every site -- each copy a place a
change lands in only one test. Factories return the MODEL, not the wire mapping."""

import pathlib
from collections.abc import Callable
from typing import Any

from app.admission.admission_links import admit_discovered_links
from app.admission.payload_helpers import payload_policy
from app.admission.policy import PolicyTier, read_policy
from app.config.settings import CrawlerSettings
from app.models.crawl_policy import CrawlPolicy
from app.models.normalized_url import NormalizedUrl
from app.safety.validation import HostPolicy
from app.storage.job_store import CrawlJobState, JobStore
from app.vocabulary.strategies import HTTP_DECLARED_CRAWLER

UNRESTRICTED_HOST_POLICY = HostPolicy.unrestricted()


def canonical(url: "str | NormalizedUrl") -> NormalizedUrl:
    """The admitted value production would hold for ``url``.

    Going through the production normalizer is the point: a hand-rolled form
    passes every test while production changes underneath it."""
    if isinstance(url, NormalizedUrl):
        return url
    return NormalizedUrl(url, policy=UNRESTRICTED_HOST_POLICY)


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

# The minimal-deployment profile: a short per-request timeout and little room for discovered links, because a
# box this small is the one where admitting a hundred extra pages is what matters.
_CRAWL_LIMITS = {"timeout_seconds": 5, "max_retries": 0, "max_discovered_links": 10}

# The live-server profile: a generous per-request timeout and room for the links a real page actually links
# to.
_STRATEGY_LIMITS = {"timeout_seconds": 10, "max_retries": 0, "max_discovered_links": 50}


def make_base_policy(strategies=None) -> CrawlPolicy:
    return read_policy(
        {
            "schema_version": "2026-06-23",
            "strategies": list(strategies) if strategies is not None else [HTTP_DECLARED_CRAWLER],
            "limits": dict(_CRAWL_LIMITS),
            "headers": {},
            "request_profile": {},
            "browser_profile": {},
            "discovery": {},
        }
    )


def _policy(extra=None) -> CrawlPolicy:
    """The strategy-level policy, with an optional override merged over it.

    Carries no ``strategies`` on purpose: strategy tests want the default
    ladder to climb, not a pinned step."""
    policy: dict[str, Any] = {
        "request_profile": {},
        "headers": {},
        "limits": dict(_STRATEGY_LIMITS),
    }
    if extra:
        policy.update(extra)
    return read_policy(policy)


def http_strategy_policy(*, extraction: str = "full", discovery: bool | None = None) -> CrawlPolicy:
    """A crawl policy for the HTTP/browser strategy tests, with discovery optional.

    ``discovery=True`` turns the channels ON by spelling them; the old all-off
    block read as truthy in one reader and false in the model."""
    policy = make_base_policy().model_dump()
    policy["extraction"] = extraction
    if discovery is not None:
        policy["discovery"] = {channel: discovery for channel in ("robots", "sitemaps", "feeds")}
    return read_policy(policy)


async def _robots_allow_all(_url: NormalizedUrl) -> bool:
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
    known: set[NormalizedUrl] | None = None,
    make_entry=lambda link: link.normalized_url,
    scope_pattern=None,
    want_document_bodies: bool = False,
    respect_robots: bool = True,
    robots_allows=None,
    robots_crawl_delay=None,
    commit_crawl_delay=None,
    host_policy: HostPolicy = UNRESTRICTED_HOST_POLICY,
):
    """Run production ``admit_discovered_links`` and return its real outcome.

    Every keyword mirrors production's, so a new required argument raises
    here once; no ``**kwargs`` -- a misspelled override is a ``TypeError``."""
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
    )


__all__ = [
    "BOOL_ENV_CASES",
    "UNRESTRICTED_HOST_POLICY",
    "_policy",
    "admit",
    "http_strategy_policy",
    "make_base_policy",
    "payload_policy",
]


def crawl_limits(*, respect_robots: bool | None = None, **over: Any) -> dict[str, Any]:
    """The limits block a crawl fixture needs, with overrides merged over it.

    ``timeout_seconds: 10`` is the live-server profile -- a too-tight fixture
    reports a transport problem. ``respect_robots`` is a keyword tests flip."""
    limits: dict[str, Any] = {"timeout_seconds": 10, "max_retries": 0, "max_discovered_links": 10}
    if respect_robots is not None:
        limits["respect_robots"] = respect_robots
    limits.update(over)
    return limits


def crawl_policy(
    strategies=None,
    *,
    limits: dict[str, Any] | None = None,
    discovery: dict[str, Any] | None = None,
    scope_regex: str | None = None,
    extraction: str | None = None,
) -> dict[str, Any]:
    """A crawl policy carrying strategies, limits, and the named extra blocks.

    Each extra block is a named keyword, so a typo'd name is a ``TypeError``;
    the dict is the WIRE shape production's ``payload_policy`` declares."""
    policy: dict[str, Any] = {
        "strategies": list(strategies) if strategies is not None else [HTTP_DECLARED_CRAWLER],
        "limits": crawl_limits(**(limits or {})),
    }
    if discovery is not None:
        policy["discovery"] = discovery
    if scope_regex is not None:
        policy["scope_regex"] = scope_regex
    if extraction is not None:
        policy["extraction"] = extraction
    return policy


def deployment_tier(**overrides: str) -> PolicyTier:
    """The operator tier a test deployment runs with.

    Required at ``JobStore``: without it, omitted limits resolve to model
    defaults. Tests that do not care about the tier default to those defaults."""
    return PolicyTier.from_settings(CrawlerSettings.from_mapping(overrides))


def job_store(
    db_path: str | pathlib.Path,
    *,
    policy: HostPolicy | None = None,
    tier: PolicyTier | None = None,
    of: type[JobStore] = JobStore,
    row_to_job: Callable[..., CrawlJobState] | None = None,
) -> JobStore:
    """A ``JobStore`` bound to a test deployment.

    Every argument a test does not pass is one it did not mean to vary; ``of``
    builds a specialized subclass through the same defaults."""
    return of(
        db_path,
        policy=policy if policy is not None else UNRESTRICTED_HOST_POLICY,
        tier=tier if tier is not None else deployment_tier(),
        row_to_job=row_to_job,
    )
