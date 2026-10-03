"""Shared recursive URL admission for the engine and durable job runner."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Iterable, MutableSequence
from dataclasses import dataclass, field
from functools import partial
from typing import Any, TypeVar
from urllib.parse import urljoin, urlsplit

from app.constants import is_analytics_host, is_document_url
from app.crawl.fetch_helpers import fetch_sitemap_urls
from app.crawl.job_store import FrontierEntry, durable_page_identity, job_max_pages
from app.crawl.policy import PolicyTier, discovery_flags, read_policy
from app.crawl.robots import FetchAndDecode, load_robots_rules, robots_allows, robots_origin
from app.crawl.scope_match import scope_allows
from app.crawl.strategies.fetchers import default_http_fetcher
from app.models.crawl_extraction_method import CrawlExtractionMethod
from app.models.crawl_policy import CrawlPolicy
from app.models.host_policy import HostPolicy
from app.models.normalized_url import NormalizedUrl
from app.safe_regex import BoundedPattern, UnsafePattern, compile_untrusted
from app.scheduler import DomainScheduler
from app.validation import safe_public_url_or_none

_logger = logging.getLogger(__name__)

RobotsAllows = Callable[[str], Awaitable[bool]]
RobotsCrawlDelay = Callable[[str], Awaitable[float | None]]
CommitCrawlDelay = Callable[[str, float], None]
T = TypeVar("T")

_MAX_SCOPE_PATTERN_LEN = 256
_ROBOTS_ADMISSION_CHUNK_SIZE = 16


def canonical_page_identity(url: str, *, host_policy: HostPolicy) -> NormalizedUrl | None:
    return safe_public_url_or_none(url, policy=host_policy)


def remaining_page_capacity(admitted_count: int, max_pages: int) -> int | None:
    """Pages still admissible, or None when the cap is UNBOUNDED.

    None is the answer for ``max_pages <= 0``, and it is the codebase's single
    definition of that sentinel -- every cap-reading site defers here rather than
    restating it. 0 means "no cap", NOT "zero pages", because the alternative
    reading makes an unlimited whole-site job (which stores 0) admit nothing and
    report no error, while a caller who means zero has ``max_pages=1`` with an
    empty frontier.

    Note the one deliberate exception: the API edge substitutes a real budget
    for an absent-or-zero request, so an externally-triggered crawl is always
    bounded. A whole-site job read back from its row keeps its 0, which is how
    the unbounded reading reaches the crawl loop.
    """
    if max_pages <= 0:
        return None
    return max(0, max_pages - max(0, admitted_count))


def compile_scope_pattern(scope_regex: Any) -> BoundedPattern | None:
    """Compile a user-supplied scope regex for bounded matching, or refuse it.

    THE definition: the crawl path (engine) and the durable admission path
    (job links) both bound a caller-supplied ``scope_regex`` through here, so
    a pattern one path refuses cannot be compiled by the other.

    Defense in depth: static shape rejection is bypassable by
    construction -- the depth-3 nested-quantifier ``(((a|aa)))*`` defeated
    every enumerated heuristic -- so the durable guarantee is BOUNDED
    EXECUTION via :mod:`app.safe_regex`: every match runs on the ``regex``
    engine with a wall-clock timeout, raising RegexTimeout instead of hanging
    the crawl loop. The cheap heuristics there remain as an early filter for
    obvious footguns; they are not load-bearing.
    """
    if not isinstance(scope_regex, str) or not scope_regex:
        return None
    try:
        return compile_untrusted(scope_regex, max_len=_MAX_SCOPE_PATTERN_LEN)
    except (UnsafePattern, re.error):
        return None


async def gather_robots_decisions(
    urls: list[str],
    robots_allows: RobotsAllows,
    robots_crawl_delay: RobotsCrawlDelay | None = None,
) -> list[tuple[bool, float | None]]:
    async def check(url: str) -> tuple[bool, float | None]:
        allowed = await robots_allows(url)
        delay = await robots_crawl_delay(url) if allowed and robots_crawl_delay is not None else None
        return allowed, delay

    decisions: list[tuple[bool, float | None]] = []
    for offset in range(0, len(urls), _ROBOTS_ADMISSION_CHUNK_SIZE):
        decisions.extend(
            await asyncio.gather(
                *(check(url) for url in urls[offset : offset + _ROBOTS_ADMISSION_CHUNK_SIZE])
            )
        )
    return decisions


async def _gather_robots_allowed(urls: list[str], robots_allows: RobotsAllows) -> list[bool]:
    return list(await asyncio.gather(*(robots_allows(url) for url in urls)))


def _is_url_text(value: Any) -> bool:
    """True for raw text or an admitted URL: the two shapes a frontier holds."""
    return isinstance(value, (str, NormalizedUrl)) and bool(value)


def _frontier_identity(value: Any, *, host_policy: HostPolicy) -> str | None:
    raw_url: Any
    if isinstance(value, FrontierEntry):
        raw_url = value.url
    elif isinstance(value, tuple) and len(value) > 1:
        raw_url = getattr(value[1], "normalized_url", None)
        if not _is_url_text(raw_url) and isinstance(value[0], str):
            raw_url = value[0]
    elif isinstance(value, dict):
        raw_url = value.get("url")
    elif isinstance(value, str):
        raw_url = value
    else:
        raw_url = getattr(value, "normalized_url", None)
    if not _is_url_text(raw_url):
        return None
    raw_url = str(raw_url)
    # A refused target gets no identity. The owner returns None for both refusal
    # reasons -- private_network_target and host_not_allowed -- and the raw
    # string is not a consolation prize: it is exactly the value the policy just
    # rejected, so falling back to it re-admits by another name. The sibling
    # helpers here (``_candidate_stream``, ``normalize_sitemap_candidates``) both
    # drop on None; this one used to hand the raw string back, which made it the
    # only place in the module where a refusal and an acceptance looked alike.
    identity = canonical_page_identity(raw_url, host_policy=host_policy)
    if identity is None:
        return None
    return str(identity)


def _candidate_stream(
    links: Iterable[Any],
    *,
    next_depth: int,
    known: set[str],
    scope_pattern: BoundedPattern | None,
    want_document_bodies: bool,
    host_policy: HostPolicy,
) -> Iterable[AdmittedLink]:
    for link in links:
        values = _link_values(link)
        if values is None:
            continue
        normalized_raw, original = values
        normalized = canonical_page_identity(normalized_raw, host_policy=host_policy)
        if normalized is None or normalized in known:
            continue
        if is_analytics_host(normalized.raw):
            continue
        if not want_document_bodies and is_document_url(normalized.raw):
            continue
        if scope_pattern is not None and not scope_allows(scope_pattern, normalized.raw):
            continue
        yield AdmittedLink(
            original_url=original,
            normalized_url=normalized,
            depth=next_depth,
        )


@dataclass(frozen=True)
class AdmittedLink:
    original_url: str
    normalized_url: NormalizedUrl
    depth: int


@dataclass(frozen=True)
class AdmissionOutcome:
    accepted: int = 0
    # Candidates the page cap REFUSED. The page is not admitted and will not be.
    cap_blocked: int = 0
    # Candidates admitted PAST the cap, which is a different event with an
    # opposite meaning: nothing was refused. Only the requeue path sets this,
    # and only because those pages were already admitted and possibly fetched
    # before the cap was reached. The two are kept apart because a caller that
    # reconciles this outcome against the frontier it can see cannot otherwise
    # tell "refused" from "admitted anyway" -- one number claimed both.
    cap_overcommitted: int = 0
    robots_blocked: int = 0
    # Candidates dropped because their canonical identity was already admitted
    # (within this call or by the job's existing state). Counted separately from
    # `cap_blocked` so a cap report never hides a dedupe decision.
    duplicates_skipped: int = 0


def _link_values(link: Any) -> tuple[str, str] | None:
    """The ``(normalized text, original text)`` an extraction record carries.

    The host is deliberately not read. ``DiscoveredLink.host`` is the extractor's
    own answer, and it disagreed with the admitted URL whenever the two were
    produced differently -- the crawl-delay key was then a host that had nothing
    to do with the request it throttled.
    """
    if isinstance(link, str):
        return link, link
    if isinstance(link, dict):
        normalized = link.get("normalized_url")
        original = link.get("url")
    else:
        normalized = getattr(link, "normalized_url", None)
        original = getattr(link, "url", None)
    if not _is_url_text(normalized):
        return None
    return (str(normalized), str(original) if _is_url_text(original) else str(normalized))


async def admit_discovered_links[T](
    links: Iterable[Any],
    *,
    source_depth: int,
    max_depth: int,
    max_pages: int,
    known: set[str],
    frontier: MutableSequence[T],
    make_entry: Callable[[AdmittedLink], T],
    scope_pattern: BoundedPattern | None,
    want_document_bodies: bool,
    respect_robots: bool,
    robots_allows: RobotsAllows,
    robots_crawl_delay: RobotsCrawlDelay | None,
    commit_crawl_delay: CommitCrawlDelay,
    host_policy: HostPolicy,
    reserve_admitted: bool = False,
    occupied: int | None = None,
) -> AdmissionOutcome:
    if source_depth >= max_depth:
        return AdmissionOutcome()
    next_depth = source_depth + 1
    normalized_known = {
        durable_page_identity(url, host_policy) for url in known if isinstance(url, str) and url
    }
    known.clear()
    known.update(normalized_known)
    reserved = set(known)
    for value in frontier:
        identity = _frontier_identity(value, host_policy=host_policy)
        if identity is not None:
            reserved.add(identity)
    current_occupied = occupied if occupied is not None else len(reserved)
    accepted = 0
    cap_blocked = 0
    robots_blocked = 0
    duplicates_skipped = 0
    stopped = False
    candidates = iter(
        _candidate_stream(
            links,
            next_depth=next_depth,
            known=known,
            scope_pattern=scope_pattern,
            want_document_bodies=want_document_bodies,
            host_policy=host_policy,
        )
    )
    while not stopped:
        capacity = remaining_page_capacity(current_occupied, max_pages)
        chunk_limit = _ROBOTS_ADMISSION_CHUNK_SIZE
        if capacity is not None:
            chunk_limit = min(chunk_limit, max(1, capacity + 1))
        chunk: list[AdmittedLink] = []
        for _ in range(chunk_limit):
            try:
                chunk.append(next(candidates))
            except StopIteration:
                break
        if not chunk:
            break
        # Drop identities the job already admitted BEFORE any per-candidate
        # work. A duplicate alias otherwise pays a robots fetch and a crawl-delay
        # commit for a page that will never be crawled.
        if reserved:
            before = len(chunk)
            chunk = [candidate for candidate in chunk if candidate.normalized_url.raw not in reserved]
            duplicates_skipped += before - len(chunk)
            if not chunk:
                continue
        if respect_robots:
            allowed_values = await _gather_robots_allowed(
                [candidate.normalized_url.raw for candidate in chunk], robots_allows
            )
        else:
            allowed_values = [True] * len(chunk)
        for candidate, allowed in zip(chunk, allowed_values, strict=True):
            if not allowed:
                robots_blocked += 1
                continue
            # `reserved` can grow inside this loop (an accepted candidate is
            # reserved), so two aliases within one chunk are caught here too.
            if candidate.normalized_url.raw in reserved:
                duplicates_skipped += 1
                continue
            delay = None
            # The crawl-delay lookup is a robots artifact: with robots disabled
            # it must not run, or a crawl that explicitly disabled robots still
            # fetched /robots.txt to learn a delay it never asked to honor.
            if respect_robots and robots_crawl_delay is not None:
                delay = await robots_crawl_delay(candidate.normalized_url.raw)
            if delay is not None and delay > 0:
                commit_crawl_delay(candidate.normalized_url.host, delay)
            if candidate.normalized_url.raw in reserved:
                continue
            capacity = remaining_page_capacity(current_occupied, max_pages)
            if capacity is not None and capacity <= 0:
                cap_blocked = 1
                stopped = True
                break
            entry = make_entry(candidate)
            frontier.append(entry)
            accepted += 1
            reserved.add(candidate.normalized_url.raw)
            if reserve_admitted:
                known.add(candidate.normalized_url.raw)
            current_occupied += 1
    return AdmissionOutcome(
        accepted=accepted,
        cap_blocked=cap_blocked,
        robots_blocked=robots_blocked,
        duplicates_skipped=duplicates_skipped,
    )


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

    async def _rules(self, url: str) -> tuple[list[tuple[bool, str]], float | None]:
        rules, _sitemaps, delay = await load_robots_rules(
            robots_origin(url, policy=self._host_policy),
            partial(default_http_fetcher, policy=self._host_policy),
            self._timeout_seconds,
            fetch_and_decode=self._fetch_and_decode,
            policy=self._host_policy,
        )
        return rules, delay

    async def allows(self, url: str) -> bool:
        rules, _delay = await self._rules(url)
        return robots_allows(url, rules)

    async def crawl_delay(self, url: str) -> float | None:
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


def admitted_page_identities(job: Any, *, host_policy: HostPolicy) -> set[str]:
    """Union of canonical identities already admitted across the job's state.

    No normalization pass: entries are canonical by construction (ingress and
    the shared admission chokepoint), and rows are normalized once at the
    storage boundary. Re-scanning the whole job here put a per-URL
    canonicalization pass on every batch's hot path.
    """
    identities: set[str] = set()
    values = set(job.visited) | {entry.url for entry in job.in_flight} | {entry.url for entry in job.frontier}
    for value in values:
        if isinstance(value, str) and value:
            identities.add(durable_page_identity(value, host_policy))
    return identities


def normalize_sitemap_candidates(
    found: Iterable[tuple[str, str | None]],
    *,
    scope_pattern: BoundedPattern | None,
    host_policy: HostPolicy,
) -> list[tuple[NormalizedUrl, str | None]]:
    candidates: list[tuple[NormalizedUrl, str | None]] = []
    seen: set[str] = set()
    for loc, lastmod in found:
        normalized = canonical_page_identity(loc, host_policy=host_policy)
        if normalized is None or normalized in seen:
            continue
        if scope_pattern is not None and not scope_allows(scope_pattern, normalized.raw):
            continue
        seen.add(str(normalized))
        candidates.append((normalized, lastmod))
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
        make_entry=lambda link: FrontierEntry(url=link.normalized_url.raw, depth=link.depth),
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
    known: set[str],
    candidates: Iterable[tuple[str, int]],
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
        ((durable_page_identity(entry.url, context.host_policy), entry.depth) for entry in entries),
    )
    job.frontier.extend(accepted_entries)
    if overcommitted:
        record_cap_overcommitted(job)
    return AdmissionOutcome(accepted=len(accepted_entries), cap_overcommitted=overcommitted)


async def fetch_job_sitemap_candidates(root_url: str, context: JobAdmissionContext) -> list[str]:
    """Sitemap candidates for a job root, or none if the root is not crawlable.

    The origin is taken from ``normalize_public_http_url`` rather than rebuilt
    here. This used to call ``urlparse`` and reassemble ``scheme://netloc``
    itself, which kept host case, kept a default port and kept a trailing dot --
    so the same job root produced a different sitemap URL depending on which
    spelling had been stored, and a host the canonicalizer refuses was used as
    a sitemap origin before any fetcher saw it. Routing through the declared
    chokepoint makes this path agree with every other URL the service handles.
    """
    from app.models.normalized_url import normalize_public_http_url

    try:
        canonical_root = normalize_public_http_url(root_url, policy=context.host_policy)
    except ValueError:
        return []
    canonical = urlsplit(canonical_root.raw)
    sitemap_url = f"{canonical.scheme}://{canonical.netloc}/sitemap.xml"
    found = await fetch_sitemap_urls(
        partial(default_http_fetcher, policy=context.host_policy),
        sitemap_url,
        context.timeout_seconds,
        host_policy=context.host_policy,
    )
    return sorted({urljoin(sitemap_url, url) for url, _lastmod in found if isinstance(url, str)})


__all__ = [
    "AdmissionOutcome",
    "AdmittedLink",
    "CommitCrawlDelay",
    "JobAdmissionContext",
    "RobotsAllows",
    "RobotsCrawlDelay",
    "RobotsGate",
    "T",
    "admit_discovered_links",
    "admit_job_links",
    "admitted_page_identities",
    "canonical_page_identity",
    "compile_scope_pattern",
    "fetch_job_sitemap_candidates",
    "gather_robots_decisions",
    "normalize_sitemap_candidates",
    "record_cap_overcommitted",
    "record_cap_truncated",
    "remaining_page_capacity",
    "requeue_admitted_pages",
]
