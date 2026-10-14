"""Link admission over the frontier: identity, scope, robots, capacity."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Iterable, MutableSequence
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
    "gather_robots_decisions",
    "remaining_page_capacity",
]

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
    next_depth = source_depth + 1
    reserved: set[NormalizedUrl] = set(known)
    for value in frontier:
        identity = _frontier_identity(value, host_policy=host_policy)
        if identity is not None:
            reserved.add(identity)
    # Occupation is this set's size, never a caller's arithmetic: known ∪
    # frontier is committed + in-flight + queued, so a hand-passed count was blind.
    current_occupied = len(reserved)
    accepted = 0
    cap_blocked = False
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
        # Drop already-admitted identities BEFORE per-candidate work, or a
        # duplicate alias pays a robots fetch and a crawl-delay commit for nothing.
        if reserved:
            before = len(chunk)
            chunk = [candidate for candidate in chunk if candidate.normalized_url not in reserved]
            duplicates_skipped += before - len(chunk)
            if not chunk:
                continue
        if respect_robots:
            allowed_values = await _gather_robots_allowed(
                [candidate.normalized_url for candidate in chunk], robots_allows
            )
        else:
            allowed_values = [True] * len(chunk)
        for candidate, allowed in zip(chunk, allowed_values, strict=True):
            if not allowed:
                robots_blocked += 1
                continue
            # `reserved` can grow inside this loop (an accepted candidate is
            # reserved), so two aliases within one chunk are caught here too.
            if candidate.normalized_url in reserved:
                duplicates_skipped += 1
                continue
            delay = None
            # The crawl-delay lookup is a robots artifact: with robots disabled it
            # must not run, or a crawl still fetches /robots.txt for a delay.
            if respect_robots and robots_crawl_delay is not None:
                delay = await robots_crawl_delay(candidate.normalized_url)
            if delay is not None and delay > 0:
                commit_crawl_delay(candidate.normalized_url.host, delay)
            if candidate.normalized_url in reserved:
                continue
            capacity = remaining_page_capacity(current_occupied, max_pages)
            if capacity is not None and capacity <= 0:
                cap_blocked = True
                stopped = True
                break
            entry = make_entry(candidate)
            frontier.append(entry)
            accepted += 1
            reserved.add(candidate.normalized_url)
            if reserve_admitted:
                known.add(candidate.normalized_url)
            current_occupied += 1
    return AdmissionOutcome(
        accepted=accepted,
        cap_blocked=cap_blocked,
        robots_blocked=robots_blocked,
        duplicates_skipped=duplicates_skipped,
    )


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
    """Compile a user-supplied scope regex for bounded matching.

    ``None`` means exactly one thing: no restriction was declared. A restriction
    that IS declared but cannot compile raises instead, because every consumer
    reads ``None`` as "allow everything" -- and a crawl the operator restricted
    running unrestricted is the one outcome a scope policy must never produce.
    The request boundary rejects invalid patterns with ``invalid_policy``, so
    the row that reaches here compiled when it was written; the gap is the
    resume, where the same row loads under today's rules.

    Defense in depth: static shape rejection is bypassable by
    construction -- the depth-3 nested-quantifier ``(((a|aa)))*`` defeated
    every enumerated heuristic -- so the durable guarantee is BOUNDED
    EXECUTION via :mod:`app.safety.safe_regex`: every match runs on the ``regex``
    engine with a wall-clock timeout, raising RegexTimeout instead of hanging
    the crawl loop. The cheap heuristics there remain as an early filter for
    obvious footguns; they are not load-bearing.
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


async def _gather_robots_allowed(urls: list[NormalizedUrl], robots_allows: RobotsAllows) -> list[bool]:
    return list(await asyncio.gather(*(robots_allows(url) for url in urls)))


def _is_url_text(value: Any) -> bool:
    """True for raw text or an admitted URL: the two shapes a frontier holds."""
    return isinstance(value, (str, NormalizedUrl)) and bool(value)


def _frontier_identity(value: Any, *, host_policy: HostPolicy) -> NormalizedUrl | None:
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
