"""Link admission over the frontier: identity, scope, robots, capacity."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Iterable, Iterator, MutableSequence
from dataclasses import dataclass
from typing import Any, TypeVar

from app.admission.scope_match import scope_allows
from app.models.host_policy import HostPolicy
from app.models.normalized_url import NormalizedUrl, admit_or_none
from app.safety.safe_regex import BoundedPattern, UnsafePattern, compile_untrusted
from app.storage.job_store import FrontierEntry
from app.vocabulary.hosts import is_analytics_host, is_document_url

__all__ = [
    "AdmissionOutcome",
    "AdmittedLink",
    "CommitCrawlDelay",
    "RobotsAllows",
    "RobotsCrawlDelay",
    "T",
    "admit_discovered_links",
    "compile_scope_pattern",
    "frontier_identity",
    "gather_robots_decisions",
    "remaining_page_capacity",
]

_logger = logging.getLogger(__name__)

RobotsAllows = Callable[[NormalizedUrl], Awaitable[bool]]
RobotsCrawlDelay = Callable[[NormalizedUrl], Awaitable[float | None]]
CommitCrawlDelay = Callable[[str, float], None]
T = TypeVar("T")
_MAX_SCOPE_PATTERN_LEN = 256
_ROBOTS_ADMISSION_CHUNK_SIZE = 16


@dataclass(frozen=True)
class AdmittedLink:
    original_url: str
    normalized_url: NormalizedUrl
    depth: int


@dataclass(frozen=True)
class AdmissionOutcome:
    accepted: int = 0
    # Whether the page cap REFUSED a candidate. The page is not admitted, and
    # the first refusal stops the loop, so this is a flag and never a count.
    cap_blocked: bool = False
    # Candidates admitted PAST the cap: nothing was refused. Only the requeue
    # path sets this, for pages already admitted (maybe fetched) before the cap.
    cap_overcommitted: int = 0
    robots_blocked: int = 0
    # Candidates dropped because their canonical identity was already admitted;
    # counted apart from cap_blocked so a cap report never hides a dedupe.
    duplicates_skipped: int = 0


def _link_values(link: Any) -> tuple[str, str] | None:
    """The ``(normalized text, original text)`` an extraction record carries.

    The host is deliberately not read: ``DiscoveredLink.host`` is the extractor's
    own answer and disagreed with the admitted URL whenever produced differently.
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


@dataclass(frozen=True)
class _ChunkOutcome:
    accepted: int
    robots_blocked: int
    duplicates_skipped: int
    cap_blocked: bool
    stopped: bool
    occupied_delta: int


@dataclass
class _AdmissionPass[T]:
    """The policy, dedupe sets, and output frontier one admission run carries.

    Built by the public entry and threaded through the chunk loop unchanged;
    ``reserved`` and ``known`` are mutated in place as candidates are admitted.
    """

    robots_allows: RobotsAllows
    respect_robots: bool
    robots_crawl_delay: RobotsCrawlDelay | None
    commit_crawl_delay: CommitCrawlDelay
    max_pages: int
    reserved: set[NormalizedUrl]
    known: set[NormalizedUrl]
    frontier: MutableSequence[T]
    make_entry: Callable[[AdmittedLink], T]
    reserve_admitted: bool


def _prepare_admission[T](
    links: Iterable[Any],
    *,
    known: set[NormalizedUrl],
    frontier: MutableSequence[T],
    next_depth: int,
    scope_pattern: BoundedPattern | None,
    want_document_bodies: bool,
    host_policy: HostPolicy,
) -> tuple[set[NormalizedUrl], int, Iterator[AdmittedLink]]:
    """The occupied set and candidate stream a link-admission pass runs over."""
    reserved: set[NormalizedUrl] = set(known)
    for value in frontier:
        identity = frontier_identity(value, host_policy=host_policy)
        if identity is not None:
            reserved.add(identity)
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
    return reserved, len(reserved), candidates


def _collect_chunk(candidates: Iterator[AdmittedLink], chunk_limit: int) -> list[AdmittedLink]:
    """Pull up to ``chunk_limit`` candidates from the admission stream."""
    chunk: list[AdmittedLink] = []
    for _ in range(chunk_limit):
        try:
            chunk.append(next(candidates))
        except StopIteration:
            break
    return chunk


async def _admit_chunk[T](
    pass_: _AdmissionPass[T],
    chunk: list[AdmittedLink],
    *,
    current_occupied: int,
) -> _ChunkOutcome:
    """Admit one chunk against robots, dedupe, and the page cap."""
    reserved = pass_.reserved
    accepted = 0
    robots_blocked = 0
    duplicates_skipped = 0
    occupied_delta = 0
    if reserved:
        before = len(chunk)
        chunk = [c for c in chunk if c.normalized_url not in reserved]
        duplicates_skipped += before - len(chunk)
        if not chunk:
            return _ChunkOutcome(accepted, robots_blocked, duplicates_skipped, False, False, 0)
    if pass_.respect_robots:
        allowed_values = await _gather_robots_allowed(
            [candidate.normalized_url for candidate in chunk], pass_.robots_allows
        )
    else:
        allowed_values = [True] * len(chunk)
    for candidate, allowed in zip(chunk, allowed_values, strict=True):
        if not allowed:
            robots_blocked += 1
            continue
        # `reserved` can grow inside this loop, so two aliases within one chunk
        # are caught here too, and the crawl-delay check is a robots artifact.
        if candidate.normalized_url in reserved:
            duplicates_skipped += 1
            continue
        if pass_.respect_robots and pass_.robots_crawl_delay is not None:
            delay = await pass_.robots_crawl_delay(candidate.normalized_url)
            if delay is not None and delay > 0:
                pass_.commit_crawl_delay(candidate.normalized_url.host, delay)
        if candidate.normalized_url in reserved:
            continue
        capacity = remaining_page_capacity(current_occupied + occupied_delta, pass_.max_pages)
        if capacity is not None and capacity <= 0:
            return _ChunkOutcome(accepted, robots_blocked, duplicates_skipped, True, True, occupied_delta)
        pass_.frontier.append(pass_.make_entry(candidate))
        accepted += 1
        reserved.add(candidate.normalized_url)
        if pass_.reserve_admitted:
            pass_.known.add(candidate.normalized_url)
        occupied_delta += 1
    return _ChunkOutcome(accepted, robots_blocked, duplicates_skipped, False, False, occupied_delta)


async def _admission_loop[T](
    pass_: _AdmissionPass[T],
    candidates: Iterator[AdmittedLink],
) -> tuple[int, bool, int, int]:
    """Consume the candidate stream chunk by chunk; returns the running tally."""
    current_occupied = len(pass_.reserved)
    accepted = 0
    cap_blocked = False
    robots_blocked = 0
    duplicates_skipped = 0
    stopped = False
    while not stopped:
        capacity = remaining_page_capacity(current_occupied, pass_.max_pages)
        chunk_limit = _ROBOTS_ADMISSION_CHUNK_SIZE
        if capacity is not None:
            chunk_limit = min(chunk_limit, max(1, capacity + 1))
        chunk = _collect_chunk(candidates, chunk_limit)
        if not chunk:
            break
        outcome = await _admit_chunk(pass_, chunk, current_occupied=current_occupied)
        accepted += outcome.accepted
        robots_blocked += outcome.robots_blocked
        duplicates_skipped += outcome.duplicates_skipped
        cap_blocked = cap_blocked or outcome.cap_blocked
        stopped = stopped or outcome.stopped
        current_occupied += outcome.occupied_delta
    return accepted, cap_blocked, robots_blocked, duplicates_skipped


async def admit_discovered_links[T](
    links: Iterable[Any],
    *,
    source_depth: int,
    max_depth: int,
    max_pages: int,
    known: set[NormalizedUrl],
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
) -> AdmissionOutcome:
    if source_depth >= max_depth:
        return AdmissionOutcome()
    reserved, _occupied, candidates = _prepare_admission(
        links,
        known=known,
        frontier=frontier,
        next_depth=source_depth + 1,
        scope_pattern=scope_pattern,
        want_document_bodies=want_document_bodies,
        host_policy=host_policy,
    )
    admission_pass = _AdmissionPass(
        robots_allows=robots_allows,
        respect_robots=respect_robots,
        robots_crawl_delay=robots_crawl_delay,
        commit_crawl_delay=commit_crawl_delay,
        max_pages=max_pages,
        reserved=reserved,
        known=known,
        frontier=frontier,
        make_entry=make_entry,
        reserve_admitted=reserve_admitted,
    )
    accepted, cap_blocked, robots_blocked, duplicates_skipped = await _admission_loop(
        admission_pass, candidates
    )
    return AdmissionOutcome(
        accepted=accepted,
        cap_blocked=cap_blocked,
        robots_blocked=robots_blocked,
        duplicates_skipped=duplicates_skipped,
    )


def remaining_page_capacity(admitted_count: int, max_pages: int) -> int | None:
    """Pages still admissible, or None when the cap is unbounded (``max_pages <= 0``).

    ``None`` is the single no-cap sentinel; 0 means "no cap", not "zero pages" --
    an unlimited whole-site job stores 0, and only the API edge substitutes a real budget.
    """
    if max_pages <= 0:
        return None
    return max(0, max_pages - max(0, admitted_count))


def compile_scope_pattern(scope_regex: Any) -> BoundedPattern | None:
    """Compile a user-supplied scope regex: ``None`` means no restriction declared.

    A declared pattern that fails to compile raises -- consumers read ``None`` as
    "allow everything". Bounded execution lives in :mod:`app.safety.safe_regex`.
    """
    if not isinstance(scope_regex, str) or not scope_regex:
        return None
    try:
        return compile_untrusted(scope_regex, max_len=_MAX_SCOPE_PATTERN_LEN)
    except (UnsafePattern, re.error) as exc:
        raise ValueError(f"scope_regex does not compile: {exc}") from exc


async def gather_robots_decisions(
    urls: list[NormalizedUrl],
    robots_allows: RobotsAllows,
    robots_crawl_delay: RobotsCrawlDelay | None = None,
) -> list[tuple[bool, float | None]]:
    async def check(url: NormalizedUrl) -> tuple[bool, float | None]:
        try:
            allowed = await robots_allows(url)
            delay = await robots_crawl_delay(url) if allowed and robots_crawl_delay is not None else None
            return allowed, delay
        except Exception:
            # One URL's robots fetch must not abort the whole batch: fail that URL
            # closed and let the rest of the decision set complete.
            _logger.warning("robots_check_failed", extra={"url": url.raw})
            return False, None

    decisions: list[tuple[bool, float | None]] = []
    for offset in range(0, len(urls), _ROBOTS_ADMISSION_CHUNK_SIZE):
        decisions.extend(
            await asyncio.gather(
                *(check(url) for url in urls[offset : offset + _ROBOTS_ADMISSION_CHUNK_SIZE])
            )
        )
    return decisions


async def _gather_robots_allowed(urls: list[NormalizedUrl], robots_allows: RobotsAllows) -> list[bool]:
    async def check(url: NormalizedUrl) -> bool:
        try:
            return await robots_allows(url)
        except Exception:
            # A single failing robots check disallows that URL without dropping the
            # batch: fail closed, matching ``gather_robots_decisions``.
            _logger.warning("robots_check_failed", extra={"url": url.raw})
            return False

    return list(await asyncio.gather(*(check(url) for url in urls)))


def _is_url_text(value: Any) -> bool:
    """True for raw text or an admitted URL: the two shapes a frontier holds."""
    return isinstance(value, (str, NormalizedUrl)) and bool(value)


def frontier_identity(value: Any, *, host_policy: HostPolicy) -> NormalizedUrl | None:
    raw_url: Any
    if isinstance(value, FrontierEntry):
        return value.url
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
    if isinstance(raw_url, NormalizedUrl):
        return raw_url
    raw_url = str(raw_url)
    # A refused target gets no identity: the raw string is the very value the
    # policy rejected, so falling back to it re-admits by another name.
    identity = admit_or_none(raw_url, policy=host_policy)
    return identity


def _candidate_stream(
    links: Iterable[Any],
    *,
    next_depth: int,
    known: set[NormalizedUrl],
    scope_pattern: BoundedPattern | None,
    want_document_bodies: bool,
    host_policy: HostPolicy,
) -> Iterable[AdmittedLink]:
    for link in links:
        values = _link_values(link)
        if values is None:
            continue
        normalized_raw, original = values
        normalized = admit_or_none(normalized_raw, policy=host_policy)
        if normalized is None or normalized in known:
            continue
        if is_analytics_host(normalized):
            continue
        if not want_document_bodies and is_document_url(normalized):
            continue
        if scope_pattern is not None and not scope_allows(scope_pattern, normalized.raw):
            continue
        yield AdmittedLink(
            original_url=original,
            normalized_url=normalized,
            depth=next_depth,
        )
