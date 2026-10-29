"""Link admission over the frontier: identity, scope, robots, capacity."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Iterable, Iterator, MutableSequence
from dataclasses import dataclass
from typing import TypeVar

from crawlutron.admission.scope_match import scope_allows
from crawlutron.models.discovered_link import DiscoveredLink
from crawlutron.models.host_policy import HostPolicy
from crawlutron.models.json_value import JsonValue as JsonValue
from crawlutron.models.normalized_url import NormalizedUrl, admit_or_none
from crawlutron.safety.safe_regex import BoundedPattern, UnsafePattern, compile_untrusted
from crawlutron.storage.job_store import FrontierEntry
from crawlutron.vocabulary.hosts import is_analytics_host, is_document_url

#: The candidate shapes link admission accepts: the extraction record itself, a
#: wire mapping, or raw text -- the three forms the stream flattens to one type.
LinkCandidate = DiscoveredLink | dict[str, JsonValue] | str

__all__ = [
    "AdmissionOutcome",
    "AdmittedLink",
    "CommitCrawlDelay",
    "LinkCandidate",
    "RobotsAllows",
    "RobotsCrawlDelay",
    "T",
    "admit_discovered_links",
    "compile_scope_pattern",
    "frontier_identity",
    "gather_deadline_bounded",
    "gather_robots_decisions",
    "remaining_page_capacity",
]

_logger = logging.getLogger(__name__)

RobotsAllows = Callable[[NormalizedUrl], Awaitable[bool]]
RobotsCrawlDelay = Callable[[NormalizedUrl], Awaitable[float | None]]
CommitCrawlDelay = Callable[[str, float], None]
T = TypeVar("T")
_MAX_SCOPE_PATTERN_LEN = 256
# One chunk carries a whole default batch: the chunk is a fan-out bound,
# not politeness -- same-origin fetches coalesce in the robots cache below.
_ROBOTS_ADMISSION_CHUNK_SIZE = 50
# Chunk groups gathered concurrently: 4 x 50 = up to 200 origins in flight.
_ROBOTS_CHUNKS_IN_FLIGHT = 4
# How long a chunk may gather robots decisions past the per-check timeout before
# the whole chunk fails closed: a slow origin must not cost chunks × timeout.
_ROBOTS_CHUNK_DEADLINE_SLACK_SECONDS = 5.0


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
    # Candidates dropped for failing URL admission itself (malformed, unsafe,
    # or off-policy) -- counted apart from the other refusals.
    invalid_skipped: int = 0
    # The identities this pass newly reserved: callers union the delta;
    # the old whole-set ``known_after`` is gone, its last consumer threads this.
    known_delta: frozenset[NormalizedUrl] = frozenset()


def _link_values(link: LinkCandidate) -> tuple[str, str] | None:
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

    ``known`` is the caller's occupied set, read-only; ``added`` is the run's own
    growth, so dedupe checks both and neither set is copied.
    """

    robots_allows: RobotsAllows
    respect_robots: bool
    robots_crawl_delay: RobotsCrawlDelay | None
    commit_crawl_delay: CommitCrawlDelay
    max_pages: int
    known: set[NormalizedUrl]
    added: set[NormalizedUrl]
    frontier: MutableSequence[T]
    make_entry: Callable[[AdmittedLink], T]
    # Per-check robots timeout feeding the chunk deadline: the bound the
    # gather would hang past, None (the legacy gather) only when unstated.
    robots_timeout_seconds: float | None = None
    # Slack past that timeout before the chunk deadline fires.
    robots_deadline_slack_seconds: float = _ROBOTS_CHUNK_DEADLINE_SLACK_SECONDS
    # Candidates dropped by the candidate stream for failing URL admission
    # (malformed, unsafe, off-policy) -- written by the stream, read at the end.
    invalid_skipped: int = 0


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
    known, added = pass_.known, pass_.added
    accepted = 0
    robots_blocked = 0
    duplicates_skipped = 0
    occupied_delta = 0
    if known or added:
        before = len(chunk)
        chunk = [c for c in chunk if c.normalized_url not in known and c.normalized_url not in added]
        duplicates_skipped += before - len(chunk)
        if not chunk:
            return _ChunkOutcome(accepted, robots_blocked, duplicates_skipped, False, False, 0)
    if pass_.respect_robots:
        # One fan-out law: the pass uses the chunked decision path, so
        # no robots gather ever fans out past the chunk bound.
        allowed_values = [
            allowed
            for allowed, _ in await gather_robots_decisions(
                [candidate.normalized_url for candidate in chunk],
                pass_.robots_allows,
                timeout_seconds=pass_.robots_timeout_seconds,
                deadline_slack_seconds=pass_.robots_deadline_slack_seconds,
            )
        ]
    else:
        allowed_values = [True] * len(chunk)
    for candidate, allowed in zip(chunk, allowed_values, strict=True):
        if not allowed:
            robots_blocked += 1
            continue
        # `added` can grow inside this loop, so two aliases within one chunk
        # are caught here too, and the crawl-delay check is a robots artifact.
        if candidate.normalized_url in known or candidate.normalized_url in added:
            duplicates_skipped += 1
            continue
        if pass_.respect_robots and pass_.robots_crawl_delay is not None:
            delay = await pass_.robots_crawl_delay(candidate.normalized_url)
            if delay is not None and delay > 0:
                pass_.commit_crawl_delay(candidate.normalized_url.site, delay)
        if candidate.normalized_url in known or candidate.normalized_url in added:
            # The await above let another writer claim it: count, then drop.
            duplicates_skipped += 1
            continue
        capacity = remaining_page_capacity(current_occupied + occupied_delta, pass_.max_pages)
        if capacity is not None and capacity <= 0:
            return _ChunkOutcome(accepted, robots_blocked, duplicates_skipped, True, True, occupied_delta)
        pass_.frontier.append(pass_.make_entry(candidate))
        accepted += 1
        added.add(candidate.normalized_url)
        occupied_delta += 1
    return _ChunkOutcome(accepted, robots_blocked, duplicates_skipped, False, False, occupied_delta)


async def _admission_loop[T](
    pass_: _AdmissionPass[T],
    candidates: Iterator[AdmittedLink],
) -> tuple[int, bool, int, int]:
    """Consume the candidate stream chunk by chunk; returns the running tally."""
    current_occupied = len(pass_.known) + len(pass_.added)
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
    links: Iterable[LinkCandidate],
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
    robots_timeout_seconds: float | None = None,
    robots_deadline_slack_seconds: float = _ROBOTS_CHUNK_DEADLINE_SLACK_SECONDS,
) -> AdmissionOutcome:
    """Admit ``links`` against the occupied set ``known`` (full: visited, in-flight,
    frontier). The run never mutates or copies it; the outcome carries
    the DELTA it added, which callers union into their own set."""
    if source_depth >= max_depth:
        return AdmissionOutcome()
    added: set[NormalizedUrl] = set()
    admission_pass = _AdmissionPass(
        robots_allows=robots_allows,
        respect_robots=respect_robots,
        robots_crawl_delay=robots_crawl_delay,
        commit_crawl_delay=commit_crawl_delay,
        max_pages=max_pages,
        known=known,
        added=added,
        frontier=frontier,
        make_entry=make_entry,
        robots_timeout_seconds=robots_timeout_seconds,
        robots_deadline_slack_seconds=robots_deadline_slack_seconds,
    )
    candidates = iter(
        _candidate_stream(
            links,
            next_depth=source_depth + 1,
            known=known,
            scope_pattern=scope_pattern,
            want_document_bodies=want_document_bodies,
            host_policy=host_policy,
            pass_=admission_pass,
        )
    )
    accepted, cap_blocked, robots_blocked, duplicates_skipped = await _admission_loop(
        admission_pass, candidates
    )
    return AdmissionOutcome(
        accepted=accepted,
        cap_blocked=cap_blocked,
        robots_blocked=robots_blocked,
        duplicates_skipped=duplicates_skipped,
        invalid_skipped=admission_pass.invalid_skipped,
        known_delta=frozenset(added),
    )


def remaining_page_capacity(admitted_count: int, max_pages: int) -> int | None:
    """Pages still admissible, or None when the cap is unbounded (``max_pages <= 0``).

    ``None`` is the single no-cap sentinel; 0 means "no cap", not "zero pages" --
    an unlimited whole-site job stores 0, and only the API edge substitutes a real budget.
    """
    if max_pages <= 0:
        return None
    return max(0, max_pages - max(0, admitted_count))


def compile_scope_pattern(scope_regex: str | JsonValue | None) -> BoundedPattern | None:
    """Compile a user-supplied scope regex: ``None`` means no restriction declared.

    A declared pattern that fails to compile raises -- consumers read ``None`` as
    "allow everything". Bounded execution lives in :mod:`crawlutron.safety.safe_regex`.
    """
    if not isinstance(scope_regex, str) or not scope_regex:
        return None
    try:
        return compile_untrusted(scope_regex, max_len=_MAX_SCOPE_PATTERN_LEN)
    except (UnsafePattern, re.error) as exc:
        raise ValueError(f"scope_regex does not compile: {exc}") from exc


async def gather_deadline_bounded[T](
    urls: list[NormalizedUrl],
    check: Callable[[NormalizedUrl], Awaitable[T]],
    fail_closed: T,
    *,
    timeout_seconds: float | None,
    deadline_slack_seconds: float = _ROBOTS_CHUNK_DEADLINE_SLACK_SECONDS,
) -> list[T]:
    """Run one chunk's robots checks under the wall-clock deadline.

    No timeout is the legacy unbounded gather; past the deadline the chunk
    fails closed so a stalled origin costs the slack, not chunks × timeout.
    """
    aws = [check(url) for url in urls]
    if timeout_seconds is None:
        return list(await asyncio.gather(*aws))
    try:
        return list(
            await asyncio.wait_for(
                asyncio.gather(*aws),
                timeout=timeout_seconds + deadline_slack_seconds,
            )
        )
    except TimeoutError:
        _logger.warning("robots_chunk_deadline_exceeded", extra={"chunk_size": len(urls)})
        return [fail_closed] * len(urls)


async def _gather_chunk(
    check: Callable[[NormalizedUrl], Awaitable[tuple[bool, float | None]]],
    chunk: list[NormalizedUrl],
    fail_closed: tuple[bool, float | None],
    timeout_seconds: float | None,
    deadline_slack_seconds: float,
) -> list[tuple[bool, float | None]]:
    """One chunk's deadline gather with T fixed to the decision shape (mypy)."""
    return await gather_deadline_bounded(
        chunk,
        check,
        fail_closed,
        timeout_seconds=timeout_seconds,
        deadline_slack_seconds=deadline_slack_seconds,
    )


async def gather_robots_decisions(
    urls: list[NormalizedUrl],
    robots_allows: RobotsAllows,
    robots_crawl_delay: RobotsCrawlDelay | None = None,
    *,
    timeout_seconds: float | None = None,
    deadline_slack_seconds: float = _ROBOTS_CHUNK_DEADLINE_SLACK_SECONDS,
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
    chunks = [
        urls[o : o + _ROBOTS_ADMISSION_CHUNK_SIZE] for o in range(0, len(urls), _ROBOTS_ADMISSION_CHUNK_SIZE)
    ]
    # Bounded-concurrent chunks: each is independently deadline-bounded, so a
    # 200-cold-origin page costs ceil(chunks/4) deadlines, not chunks.
    for group in (
        chunks[i : i + _ROBOTS_CHUNKS_IN_FLIGHT] for i in range(0, len(chunks), _ROBOTS_CHUNKS_IN_FLIGHT)
    ):
        group_results = await asyncio.gather(
            *(
                _gather_chunk(check, chunk, (False, None), timeout_seconds, deadline_slack_seconds)
                for chunk in group
            )
        )
        for chunk_decisions in group_results:
            decisions.extend(chunk_decisions)
    return decisions


def _is_url_text(value: JsonValue) -> bool:
    """True for raw text or an admitted URL: the two shapes a frontier holds."""
    return isinstance(value, (str, NormalizedUrl)) and bool(value)


def frontier_identity(
    value: LinkCandidate | FrontierEntry, *, host_policy: HostPolicy
) -> NormalizedUrl | None:
    raw_url: JsonValue
    if isinstance(value, FrontierEntry):
        return value.url
    if isinstance(value, dict):
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


def _candidate_stream[T](
    links: Iterable[LinkCandidate],
    *,
    next_depth: int,
    known: set[NormalizedUrl],
    scope_pattern: BoundedPattern | None,
    want_document_bodies: bool,
    host_policy: HostPolicy,
    pass_: _AdmissionPass[T],
) -> Iterable[AdmittedLink]:
    for link in links:
        values = _link_values(link)
        if values is None:
            continue
        normalized_raw, original = values
        normalized = admit_or_none(normalized_raw, policy=host_policy)
        if normalized is None:
            pass_.invalid_skipped += 1
            continue
        if normalized in known:
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
