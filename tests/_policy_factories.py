"""Crawl-policy defaults, and the one way a test should admit discovered links.

The policy sub-mappings were re-typed at every site -- each copy a place a
change lands in only one test. Factories return the MODEL, not the wire mapping."""

from __future__ import annotations

import pathlib
from collections.abc import Callable
from typing import TYPE_CHECKING

from safe_url.host_policy import HostPolicy
from safe_url.normalized_url import NormalizedUrl

from crawlutron.admission.admission_links import admit_discovered_links
from crawlutron.admission.payload_helpers import payload_policy
from crawlutron.admission.policy import PolicyTier, read_policy
from crawlutron.storage.job_store import CrawlJobState, JobStore

if TYPE_CHECKING:
    # Annotation-only names, imported for the type checker (deferred annotations
    # keep full checking); the factory never constructs these at runtime.
    from crawlutron.models.crawl_policy import CrawlPolicy
    from crawlutron.models.json_value import JsonValue

UNRESTRICTED_HOST_POLICY = HostPolicy.unrestricted()


def wire_policy(
    *,
    strategies: list[str] | None = None,
    limits: dict[str, JsonValue] | None = None,
    discovery: dict[str, JsonValue] | None = None,
    scope_regex: str | None = None,
    extraction: str | None = None,
) -> dict[str, JsonValue]:
    """The test-side wire shape: each block is a named keyword, so a typo'd name
    is a ``TypeError``. The model's own defaults apply at ``read_policy``."""
    from crawlutron.vocabulary.strategies import HTTP_DECLARED_CRAWLER

    policy: dict[str, JsonValue] = {
        "strategies": list(strategies) if strategies is not None else [HTTP_DECLARED_CRAWLER],
        "limits": {} if limits is None else dict(limits),
    }
    if discovery is not None:
        policy["discovery"] = discovery
    if scope_regex is not None:
        policy["scope_regex"] = scope_regex
    if extraction is not None:
        policy["extraction"] = extraction
    return policy


def canonical(url: str | NormalizedUrl) -> NormalizedUrl:
    """The admitted value production would hold for ``url``.

    Going through the production normalizer is the point: a hand-rolled form
    passes every test while production changes underneath it."""
    if isinstance(url, NormalizedUrl):
        return url
    return NormalizedUrl(url, policy=UNRESTRICTED_HOST_POLICY)


def admitted(url: str, *, policy: HostPolicy) -> NormalizedUrl:
    """A URL admitted under a policy the test names, through the production
    constructor -- the same one production builds every page identity with."""
    return NormalizedUrl(url, policy=policy)


def host_policy_for(allowed: str, blocked: str) -> HostPolicy:
    """The restricted admission policy a test pins, built through the model's
    own factory -- not a re-derived allow/deny pair."""
    return HostPolicy.from_strings(allowed, blocked)


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
        wire_policy(
            strategies=strategies,
            limits=dict(_CRAWL_LIMITS),
        )
    )


def _policy(extra=None) -> CrawlPolicy:
    """The strategy-level policy, with an optional override merged over it.

    Carries no ``strategies`` on purpose: strategy tests want the default
    ladder to climb, not a pinned step."""
    policy = wire_policy(limits=dict(_STRATEGY_LIMITS))
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
    robots_timeout_seconds: float | None = None,
    robots_deadline_slack_seconds: float = 5.0,
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
        robots_timeout_seconds=robots_timeout_seconds,
        robots_deadline_slack_seconds=robots_deadline_slack_seconds,
        host_policy=host_policy,
    )


__all__ = [
    "BOOL_ENV_CASES",
    "UNRESTRICTED_HOST_POLICY",
    "_policy",
    "admit",
    "admitted",
    "canonical",
    "crawl_limits",
    "crawl_policy",
    "host_policy_for",
    "http_strategy_policy",
    "make_base_policy",
    "payload_policy",
]


def crawl_policy(strategies=None, **over: JsonValue) -> dict[str, JsonValue]:
    """The wire policy a test fixture sends: the live-server limits by default,
    any wire block overridden through ``over``. The grammar lives in
    ``tests._policy_factories.wire_policy``; this names a profile only."""
    return {**wire_policy(strategies=strategies, limits=crawl_limits()), **over}


def crawl_limits(*, respect_robots: bool | None = None, **over: JsonValue) -> dict[str, JsonValue]:
    """The limits block a crawl fixture needs, with overrides merged over it.

    ``timeout_seconds: 10`` is the live-server profile -- a too-tight fixture
    reports a transport problem. ``respect_robots`` is a keyword tests flip."""
    limits: dict[str, JsonValue] = {"timeout_seconds": 10, "max_retries": 0, "max_discovered_links": 10}
    if respect_robots is not None:
        limits["respect_robots"] = respect_robots
    limits.update(over)
    return limits


def deployment_tier(**overrides: str) -> PolicyTier:
    """The operator tier a test deployment runs with.

    Required at ``JobStore``: without it, omitted limits resolve to model
    defaults. Tests that do not care about the tier default to those defaults."""
    from crawlutron.config.settings import CrawlerSettings

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
