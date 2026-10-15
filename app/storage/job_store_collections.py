"""Persisted job collections: entries, status vocabulary, normalization.

A row is ``json.loads`` of TEXT, so every read path admits what it finds and
drops what does not survive admission. Identity is derived on read, never
trusted from a stored flag.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, TypedDict

from app.admission.policy import PolicyTier, read_policy
from app.models.crawl_policy import CrawlPolicy
from app.models.host_policy import HostPolicy
from app.models.normalized_url import NormalizedUrl, admit_or_none


class JobKind(StrEnum):
    """Kind of crawl job."""

    WHOLE_SITE = "whole_site"
    SINGLE_SHOT = "single_shot"


class JobStatus(StrEnum):
    """Lifecycle status of a crawl job."""

    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


#: The states a compare-and-set accepts: one status, or the set a transition is
#: legal from. Named once so the sync method and its twin cannot diverge.
type _Expected = JobStatus | frozenset[JobStatus]

#: The states a job never leaves. PAUSED is absent: stopped is not finished.
TERMINAL_STATUSES = frozenset({JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED})


@dataclass(frozen=True)
class FrontierEntry:
    """One canonical URL and its admitted crawl depth."""

    url: NormalizedUrl
    depth: int

    def to_dict(self) -> FrontierEntryDict:
        return {"url": self.url.raw, "depth": self.depth}


class FrontierEntryDict(TypedDict):
    """The stored wire form of :class:`FrontierEntry`."""

    url: str
    depth: int


# A persisted row is ``json.loads`` of TEXT, so collections may be absent, bare
# or corrupt. ``object`` not ``Any``: it keeps the isinstance narrowing visible.
_PersistedJSON = object


def job_max_pages(policy: Any, tier: PolicyTier | None = None) -> int:
    return read_policy(policy, tier).limits.max_pages


def _entry_depth(value: Any, fallback: int) -> int:
    if isinstance(value, bool):
        return fallback
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return fallback


def _entry_payload(entry: Any) -> Any:
    """Serialize a frontier entry, tolerating the legacy bare-string shape.

    The store does not rewrite a caller's collections: a row written with bare
    strings stays bare, and :meth:`CrawlJobState.from_json` recognizes that
    shape as legacy and normalizes it once on read.
    """
    return entry.to_dict() if isinstance(entry, FrontierEntry) else entry


def normalize_frontier(
    values: _PersistedJSON,
    *,
    root_url: str | NormalizedUrl | None,
    policy: CrawlPolicy,
    host_policy: HostPolicy,
) -> list[FrontierEntry]:
    """Coerce persisted/runner-supplied frontier values into ``FrontierEntry`` records.

    Canonicalization is unconditional on the read path: it coerces shape, keeps
    the shallowest depth per identity, and drops unusable entries. A persisted
    "already canonical" flag could disagree with the row it describes, which is
    why migration 0002 dropped it.
    """
    if not isinstance(values, (list, tuple)):
        return []
    canonical_root = (
        root_url if isinstance(root_url, NormalizedUrl) else admit_or_none(root_url, policy=host_policy)
    )
    legacy_depth = policy.limits.max_depth
    entries: dict[NormalizedUrl, FrontierEntry] = {}
    order: list[NormalizedUrl] = []

    def _keep_shallowest(url: NormalizedUrl, entry_depth: int) -> None:
        """First sighting keeps its slot; a later shallower one replaces it."""
        previous = entries.get(url)
        if previous is None:
            entries[url] = FrontierEntry(url=url, depth=entry_depth)
            order.append(url)
        elif entry_depth < previous.depth:
            entries[url] = FrontierEntry(url=url, depth=entry_depth)

    for value in values:
        if isinstance(value, FrontierEntry):
            entry = value
            entry_depth = 0 if entry.url == canonical_root else _entry_depth(entry.depth, legacy_depth)
            _keep_shallowest(entry.url, entry_depth)
            continue
        if isinstance(value, dict):
            raw_url = value.get("url")
            depth = value.get("depth", legacy_depth)
        else:
            raw_url = value
            depth = legacy_depth
        if not isinstance(raw_url, str) or not raw_url:
            continue
        url = admit_or_none(raw_url, policy=host_policy)
        if url is None:
            continue
        entry_depth = 0 if url == canonical_root else _entry_depth(depth, legacy_depth)
        _keep_shallowest(url, entry_depth)
    return [entries[url] for url in order]


def _normalize_visited(visited: _PersistedJSON, *, host_policy: HostPolicy) -> set[NormalizedUrl]:
    """Coerce a persisted visited column to admitted values, keeping only what admits.

    Visited elements arrive both ways: ``from_json`` admits its list before
    this pass, while raw rows still carry bare text. An admitted value keeps
    its identity by construction; text must pass admission. A persisted column
    that is not a sequence of URLs is not a crash worth raising: the job loses
    its visited history, and the frontier still bounds what it will fetch.
    """
    if isinstance(visited, str):
        visited_values: list[object] = [visited]
    elif isinstance(visited, Iterable):
        visited_values = list(visited)
    else:
        visited_values = []
    normalized: set[NormalizedUrl] = set()
    for url in visited_values:
        if isinstance(url, NormalizedUrl):
            normalized.add(url)
        elif isinstance(url, str) and url:
            admitted = admit_or_none(url, policy=host_policy)
            if admitted is not None:
                normalized.add(admitted)
    return normalized


def normalize_job_collections(
    *,
    root_url: str,
    policy: CrawlPolicy,
    visited: _PersistedJSON,
    frontier: _PersistedJSON,
    in_flight: _PersistedJSON,
    host_policy: HostPolicy,
) -> tuple[set[NormalizedUrl], list[FrontierEntry], list[FrontierEntry]]:
    normalized_visited = _normalize_visited(visited, host_policy=host_policy)
    # normalize_frontier admits the root text itself; the str here is the
    # row's durable identity, not a live URL value.
    normalized_frontier = normalize_frontier(
        frontier,
        root_url=root_url,
        policy=policy,
        host_policy=host_policy,
    )
    normalized_in_flight = normalize_frontier(
        in_flight,
        root_url=root_url,
        policy=policy,
        host_policy=host_policy,
    )
    frontier_by_url = {entry.url: entry for entry in normalized_frontier}
    normalized_in_flight = [
        FrontierEntry(
            url=entry.url,
            depth=min(entry.depth, frontier_by_url[entry.url].depth)
            if entry.url in frontier_by_url
            else entry.depth,
        )
        for entry in normalized_in_flight
        if entry.url not in normalized_visited
    ]
    in_flight_ids = {entry.url for entry in normalized_in_flight}
    normalized_frontier = [
        entry
        for entry in normalized_frontier
        if entry.url not in normalized_visited and entry.url not in in_flight_ids
    ]
    return normalized_visited, normalized_frontier, normalized_in_flight


__all__ = [
    "TERMINAL_STATUSES",
    "FrontierEntry",
    "JobKind",
    "JobStatus",
    "job_max_pages",
    "normalize_frontier",
    "normalize_job_collections",
]
