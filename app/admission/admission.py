"""Job admission: gates, sitemap candidates, caps and requeue."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from app.admission.admission_links import (
    AdmissionOutcome,
    admit_discovered_links,
    compile_scope_pattern,
    remaining_page_capacity,
)
from app.admission.policy import PolicyTier, discovery_flags, read_policy
from app.admission.scope_match import scope_allows
from app.models.crawl_extraction_method import CrawlExtractionMethod
from app.models.crawl_policy import CrawlPolicy
from app.models.host_policy import HostPolicy
from app.models.normalized_url import NormalizedUrl, admit_or_none
from app.orchestration.fetch_helpers import fetch_sitemap_urls
from app.orchestration.robots import FetchAndDecode, load_robots_rules, robots_allows
from app.orchestration.scheduler import DomainScheduler
from app.safety.safe_regex import BoundedPattern
from app.storage.job_store import FrontierEntry, job_max_pages
from app.transport.fetchers import default_http_fetcher

_logger = logging.getLogger(__name__)


class RobotsGate:
    def __init__(
        self,
        policy: CrawlPolicy,
        host_policy: HostPolicy,
        fetch_and_decode: FetchAndDecode | None = None,
    ) -> None:
        self._host_policy = host_policy
        self._timeout_seconds = policy.limits.timeout_seconds
        self._fetch_and_decode = fetch_and_decode

    async def _rules(self, url: NormalizedUrl) -> tuple[list[tuple[bool, str]], float | None]:
        rules, _sitemaps, delay = await load_robots_rules(
            url,
            default_http_fetcher,
            self._timeout_seconds,
            fetch_and_decode=self._fetch_and_decode,
            policy=self._host_policy,
        )
        return rules, delay

    async def allows(self, url: NormalizedUrl) -> bool:
        rules, _delay = await self._rules(url)
        return robots_allows(url, rules)

    async def crawl_delay(self, url: NormalizedUrl) -> float | None:
        _rules, delay = await self._rules(url)
        return delay


@dataclass
class JobAdmissionContext:
    policy: CrawlPolicy
    host_policy: HostPolicy
    max_depth: int
    max_pages: int
    scope_pattern: BoundedPattern | None
    want_document_bodies: bool
    respect_robots: bool
    seed_sitemaps: bool
    timeout_seconds: int
    robots: RobotsGate
    scheduler: DomainScheduler
    delayed_hosts: set[str] = field(default_factory=set)

    def commit_crawl_delay(self, domain: str, delay: float) -> None:
        self.scheduler.set_domain_min_delay(domain, delay)
        self.delayed_hosts.add(domain.lower())

    @classmethod
    def create(
        cls,
        policy: Any,
        tier: PolicyTier,
        settings: Any,
        fetch_and_decode: FetchAndDecode | None = None,
    ) -> JobAdmissionContext:
        """The admission gate's own view of a policy.

        ``tier`` and ``settings`` are separate because they answer different
        questions: the tier is what an omitted limit means for this deployment,
        and ``settings`` is the scheduler's own configuration. Passing settings
        where a tier belongs is what let the job path resolve its limits with
        the model defaults while ``/v1/crawl`` used the operator tier.
        """
        normalized = read_policy(policy, tier)
        limits = normalized.limits
        host_policy = HostPolicy.from_settings(settings)
        robots = RobotsGate(normalized, host_policy, fetch_and_decode)
        scheduler = DomainScheduler(
            global_limit=max(1, int(getattr(settings, "global_concurrency", 4))),
            per_domain_limit=max(1, int(getattr(settings, "per_domain_concurrency", 2))),
            delay_seconds=max(0.0, float(getattr(settings, "per_domain_delay_ms", 500)) / 1000),
            max_rps=limits.max_rps,
            adaptive=bool(getattr(settings, "adaptive_throttling", True)),
            adaptive_min_delay=float(getattr(settings, "adaptive_min_delay", 0.5)),
            adaptive_max_delay=float(getattr(settings, "adaptive_max_delay", 30.0)),
            adaptive_latency_factor=float(getattr(settings, "adaptive_latency_factor", 1.0)),
        )
        discovery = discovery_flags(normalized)
        return cls(
            policy=normalized,
            host_policy=host_policy,
            max_depth=limits.max_depth,
            max_pages=limits.max_pages,
            scope_pattern=compile_scope_pattern(normalized.scope_regex),
            want_document_bodies=normalized.extraction is CrawlExtractionMethod.DOCUMENT,
            respect_robots=limits.respect_robots,
            seed_sitemaps=discovery.sitemaps,
            timeout_seconds=limits.timeout_seconds,
            robots=robots,
            scheduler=scheduler,
        )


def admitted_page_identities(job: Any, *, host_policy: HostPolicy) -> set[NormalizedUrl]:
    """Union of canonical identities already admitted across the job's state.

    No normalization pass: entries are canonical by construction (ingress and
    the shared admission chokepoint), and rows are normalized once at the
    storage boundary. Re-scanning the whole job here put a per-URL
    canonicalization pass on every batch's hot path.
    """
    identities: set[NormalizedUrl] = set()
    values = set(job.visited) | {entry.url for entry in job.in_flight} | {entry.url for entry in job.frontier}
    for value in values:
        if isinstance(value, NormalizedUrl):
            identities.add(value)
        elif isinstance(value, str) and value:
            admitted = admit_or_none(value, policy=host_policy)
            if admitted is not None:
                identities.add(admitted)
    return identities


def normalize_sitemap_candidates(
    found: Iterable[tuple[NormalizedUrl, str | None]],
    *,
    scope_pattern: BoundedPattern | None,
) -> list[tuple[NormalizedUrl, str | None]]:
    """Dedupe and scope-filter already-admitted sitemap seed candidates.

    The pairs arrive as admitted values -- resolved at the one place that knew
    their base, ``fetch_sitemap_urls`` -- so this funnel only dedupes and applies
    the job's scope pattern; there is no admission step left to re-spell, and a
    candidate refused by policy never reaches this far.
    """
    candidates: list[tuple[NormalizedUrl, str | None]] = []
    seen: set[NormalizedUrl] = set()
    for loc, lastmod in found:
        if loc in seen:
            continue
        if scope_pattern is not None and not scope_allows(scope_pattern, loc.raw):
            continue
        seen.add(loc)
        candidates.append((loc, lastmod))
    return candidates


def _record_cap_flag(job: Any, stat_key: str, level: int, event: str) -> None:
    """Set a once-only cap stat on ``job``, logging it the first time only.

    ``cap_truncated`` and ``cap_overcommitted`` are the same signal at two
    severities: the cap bit, a one-shot log so a capped crawl is visible in
    aggregate without filling the log, and no change to the job's own fate.
    Spelled per flag, the pair duplicated the log payload and the cap source
    and invited a third hand-written copy of the same three facts.

    Private because the two cap signals are the whole of this module's cap
    vocabulary. A public four-argument version would let a caller pair a stat
    key with the wrong log event, and a mismatch there is invisible -- the
    stat and the log would simply disagree.

    ``level`` is the stdlib logging level and ``event`` the record's event
    name; both are part of the signal's identity, not of this helper.
    """
    if job.stats.get(stat_key, 0) == 0:
        _logger.log(
            level,
            event,
            extra={"job_id": job.job_id, "max_pages": job_max_pages(job.policy, job.tier)},
        )
    job.stats[stat_key] = 1


def record_cap_truncated(job: Any) -> None:
    """The crawl hit its page cap and stopped adding pages."""
    _record_cap_flag(job, "cap_truncated", logging.INFO, "job_runner_cap_truncated")


def record_cap_overcommitted(job: Any) -> None:
    """The crawl was admitted past its cap; the overflow is reported, not refused."""
    _record_cap_flag(job, "cap_overcommitted", logging.WARNING, "job_runner_cap_overcommitted")


async def admit_job_links(
    job: Any,
    links: Iterable[Any],
    *,
    source_depth: int,
    context: JobAdmissionContext,
) -> AdmissionOutcome:
    known = admitted_page_identities(job, host_policy=context.host_policy)
    outcome = await admit_discovered_links(
        links,
        source_depth=source_depth,
        max_depth=context.max_depth,
        max_pages=context.max_pages,
        known=known,
        frontier=job.frontier,
        make_entry=lambda link: FrontierEntry(url=link.normalized_url, depth=link.depth),
        scope_pattern=context.scope_pattern,
        want_document_bodies=context.want_document_bodies,
        respect_robots=context.respect_robots,
        robots_allows=context.robots.allows,
        robots_crawl_delay=context.robots.crawl_delay,
        commit_crawl_delay=context.commit_crawl_delay,
        host_policy=context.host_policy,
        reserve_admitted=True,
    )
    if outcome.cap_blocked:
        record_cap_truncated(job)
    return outcome


def _requeue_page_identities(
    job: Any,
    known: set[NormalizedUrl],
    candidates: Iterable[tuple[NormalizedUrl, int]],
) -> tuple[list[FrontierEntry], int]:
    """Re-admit already-admitted identities, counting how far past the cap they go.

    The ONLY path in this module that can admit past the page cap, and it is
    deliberately so. Every other route to the frontier -- ``admit_discovered_links``
    -- refuses past the cap and reports the refusal. This one does not, because
    the pages it is given were admitted before the cap was reached, may already
    have been fetched once, and are being requeued for a transient retry.
    Refusing them now would strand work that has already been paid for.

    The count is returned rather than folded into the caller's outcome under the
    old name, because it is not a refusal: the entries are appended either way.
    Naming it the way the normal path names its refusals meant an outcome could
    report a refusal for an entry that was simultaneously present in the frontier.

    ``known`` is mutated as identities are admitted, so a caller passing the same
    identity twice admits it once.
    """
    accepted: list[FrontierEntry] = []
    overcommitted = 0
    max_pages = job_max_pages(job.policy, job.tier)
    for identity, depth in candidates:
        if identity in known:
            continue
        capacity = remaining_page_capacity(len(known), max_pages)
        if capacity is not None and capacity <= 0:
            overcommitted += 1
        accepted.append(FrontierEntry(url=identity, depth=depth))
        known.add(identity)
    return accepted, overcommitted


def requeue_admitted_pages(
    job: Any,
    entries: Iterable[FrontierEntry],
    context: JobAdmissionContext,
) -> AdmissionOutcome:
    known = admitted_page_identities(job, host_policy=context.host_policy)
    accepted_entries, overcommitted = _requeue_page_identities(
        job,
        known,
        ((entry.url, entry.depth) for entry in entries),
    )
    job.frontier.extend(accepted_entries)
    if overcommitted:
        record_cap_overcommitted(job)
    return AdmissionOutcome(accepted=len(accepted_entries), cap_overcommitted=overcommitted)


async def fetch_job_sitemap_candidates(root_url: str, context: JobAdmissionContext) -> list[str]:
    """Sitemap candidates for a job root, or none if the root is not crawlable.

    The origin is taken from the admitted ``NormalizedUrl`` rather than rebuilt
    here. This used to call ``urlparse`` and reassemble ``scheme://netloc``
    itself, which kept host case, kept a default port and kept a trailing dot --
    so the same job root produced a different sitemap URL depending on which
    spelling had been stored, and a host the canonicalizer refuses was used as
    a sitemap origin before any fetcher saw it. Routing through the declared
    chokepoint makes this path agree with every other URL the service handles.
    """
    try:
        canonical_root = NormalizedUrl(root_url, policy=context.host_policy)
    except ValueError:
        return []
    sitemap_url = admit_or_none(f"{canonical_root.origin}/sitemap.xml", policy=context.host_policy)
    if sitemap_url is None:
        return []
    found = await fetch_sitemap_urls(
        default_http_fetcher,
        sitemap_url,
        context.timeout_seconds,
        host_policy=context.host_policy,
    )
    return sorted({loc.raw for loc, _lastmod in found})


__all__ = [
    "JobAdmissionContext",
    "RobotsGate",
    "admit_job_links",
    "admitted_page_identities",
    "fetch_job_sitemap_candidates",
    "normalize_sitemap_candidates",
    "record_cap_overcommitted",
    "record_cap_truncated",
    "requeue_admitted_pages",
]
