"""Crawl-policy defaults, and the one way a test should admit discovered links.

A crawl policy is a handful of sub-mappings that are only ever spelled out
together, and the values in them were re-typed at every site: this file alone
carried the same limits block twice and a third copy of a sibling, and
``admit_discovered_links`` was called with all fourteen of its keywords written
out at each of the three sites that needed it. Each copy is a place a policy
change lands in only one of them, and the copy is the one the test runs.

``_CRAWL_LIMITS`` and ``_STRATEGY_LIMITS`` are the two profiles, named so the
factories below differ only in what is actually different about them, and every
factory hands out a fresh value because callers are told to derive their own
from the policy they are given.

The factories return the model, not the wire mapping, because that is what every
function they feed now takes. A test that built the dict itself had a way to
produce a policy no caller could -- which is why a suite could stay green while
the production read edge answered a different question than the test's did.
"""

import pathlib
from typing import Any

from app.api_models import CrawlPolicy
from app.constants import HTTP_DECLARED_CRAWLER
from app.crawl.admission import admit_discovered_links
from app.crawl.job_store import JobStore
from app.crawl.policy import PolicyTier, read_policy
from app.models import NormalizedUrl
from app.settings import CrawlerSettings
from app.validation import HostPolicy, normalize_public_http_url

UNRESTRICTED_HOST_POLICY = HostPolicy.unrestricted()


def normalized(url: str) -> NormalizedUrl:
    """The canonical form production would actually hold for ``url``.

    ``NormalizedUrl`` is a ``NewType``, so it is a claim, not a wrapper: the
    production comment at its construction site says everything upstream is raw
    input and everything downstream that names this shape is typed. A test that
    spells ``normalized_url="http://a/"`` is asserting a state production cannot
    reach -- normalization strips that trailing slash, so the real task field
    reads ``http://a`` -- and the tests were green only because nothing checked
    the claim.

    Going through the production normalizer rather than trimming the slash here
    is the whole point. A hand-rolled version of the canonical rules would be a
    second copy that passes every test while production changes underneath it,
    which is the failure mode ``UNRESTRICTED_HOST_POLICY`` next to this exists to
    prevent. It also costs nothing: the private-target guard is skipped outright
    while the suite-wide loopback fixture is in force, so no DNS happens for
    fixture hostnames.

    Lives here rather than in a URL module because it is unusable without the
    policy above: an unrestricted policy is what lets a fixture name a host like
    ``a`` at all.
    """
    return normalize_public_http_url(url, policy=UNRESTRICTED_HOST_POLICY)


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

    Carries no ``strategies`` on purpose: the strategy-level tests want the
    default ladder to climb, and a pinned strategy here would answer a question
    the ladder is what the caller is actually testing.
    """
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

    ``discovery=True`` means the channels are ON, and says so by turning them
    on. It used to write ``{"robots": False, "sitemaps": False, "feeds":
    False}`` and rely on the block being *present*: a non-empty mapping is
    truthy, so ``bool(policy["discovery"] or {})`` read an all-off block as
    "discovery requested". That was true of the old reader and false of the
    model, and the model won -- ``DiscoveryConfig`` defaults every channel
    off and the consumer asks ``any(flags)``, so the same policy discovered
    links on the browser path and not on the HTTP one. Post-fetch link
    discovery is also gated on a depth to spend, which this factory's base
    limits leave at 0, so an all-off block meant no links at all.
    """
    policy = make_base_policy().model_dump()
    policy["extraction"] = extraction
    if discovery is not None:
        policy["discovery"] = {channel: discovery for channel in ("robots", "sitemaps", "feeds")}
    return read_policy(policy)


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


def crawl_policy(
    strategies=None,
    *,
    limits: dict | None = None,
    discovery: dict | None = None,
    scope_regex: str | None = None,
    extraction: str | None = None,
) -> dict:
    """A crawl policy carrying strategies, limits, and the named extra blocks.

    The shape several tests each rebuilt around the same limits block. Each extra
    block is a named keyword rather than ``**extra``, so what a policy can carry
    is a decision made here instead of one each caller makes -- and so a typo'd
    block name is a TypeError rather than a policy that silently does not scope.

    ``scope_regex`` is omitted from the policy entirely when not given, rather
    than written as ``None``: the payload validators distinguish the two, and a
    test that did not mean to scope should not hand production a key that says
    "no scope" in a way the engine has to special-case.
    """
    policy: dict = {
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

    ``tier`` is a required argument to ``JobStore`` because a store that cannot
    say which deployment it belongs to resolves a stored row's omitted limits to
    the model defaults instead of the operator's -- and every job-path read used
    to make exactly that substitution, so ``/v1/crawl`` and ``/v1/jobs`` answered
    with different timeouts from the same configuration. Required at the store
    is how "forgot to pass the deployment" stopped being expressible; this is
    the one place a test says what its deployment is.

    Defaults to the model defaults (an empty mapping), so a test that does not
    care about the tier reads as a test about the thing it does care about.
    """
    return PolicyTier.from_settings(CrawlerSettings.from_mapping(overrides))


def job_store(
    db_path: str | pathlib.Path, *, policy: HostPolicy | None = None, tier: PolicyTier | None = None
) -> JobStore:
    """A ``JobStore`` bound to a test deployment.

    Every argument a test does not pass is the one it did not mean to vary, which
    is why the unrestricted host policy is the default here rather than repeated
    at each of the two dozen sites that construct a store.

    ``str | Path`` because that is what ``JobStore`` takes: a test that hands it
    a ``tmp_path`` was always right, and the narrower annotation here was the
    only thing rejecting it.
    """
    return JobStore(
        db_path,
        policy=policy if policy is not None else UNRESTRICTED_HOST_POLICY,
        tier=tier if tier is not None else deployment_tier(),
    )
