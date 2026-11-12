"""Persisted job collections: entries, status vocabulary, normalization.
A row is ``json.loads`` of TEXT, so every read path admits what it finds and
drops what does not survive; identity is derived on read, never from a stored flag.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from typing import TypedDict

from crawlutron.admission.policy import PolicyTier, read_policy
from crawlutron.models.crawl_policy import CrawlPolicy
from crawlutron.models.host_policy import HostPolicy
from crawlutron.models.json_value import JsonValue as JsonValue
from crawlutron.models.normalized_url import NormalizedUrl, admit_or_none


class OrderedUrlSet(dict[NormalizedUrl, None]):
    """A set of admitted URLs that remembers insertion order.

    The persist delta slices the collection by a high-water position,
    which only a stable order makes sound; reads behave like the set replaced.
    """

    def __init__(self, urls: Iterable[NormalizedUrl] = ()) -> None:
        super().__init__()
        for url in urls:
            self[url] = None

    def add(self, url: NormalizedUrl) -> None:
        """Admit one URL at the end of the order (a repeat keeps its position)."""
        self[url] = None

    def discard(self, url: NormalizedUrl) -> None:
        """Drop one URL; the remaining members keep their relative order."""
        self.pop(url, None)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, (set, frozenset)):
            return set(self) == other
        if isinstance(other, OrderedUrlSet):
            return dict.__eq__(self, other)
        return NotImplemented

    def __repr__(self) -> str:
        return f"OrderedUrlSet({list(self)!r})"


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
type Expected = JobStatus | frozenset[JobStatus]

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


#: The historical bare-text entry a pre-entry-shape row carried. The reader no
#: longer forgives it (the write side cannot emit it); it names what was lost.
type _PersistedJSON = object


def job_max_pages(policy: CrawlPolicy | dict[str, JsonValue] | None, tier: PolicyTier | None = None) -> int:
    return read_policy(policy, tier).limits.max_pages


@lru_cache(maxsize=4096)
def _admit_cached(raw: str, policy: HostPolicy) -> NormalizedUrl | None:
    """Admit ``raw`` once per (raw, policy) pair; repeats read the cache.

    Stored values were admitted at write, so re-admitting on every load is pure
    re-work; refusals are cached too, so a hostile raw stays refused on each read.
    """
    return admit_or_none(raw, policy=policy)


def read_transient_retries(retries: dict[str, int], *, host_policy: HostPolicy) -> dict[NormalizedUrl, int]:
    """The retry column's text keys re-admitted against the live policy."""
    admitted_retries: dict[NormalizedUrl, int] = {}
    for url, attempts in retries.items():
        if not isinstance(url, str):
            continue
        try:
            key = _admit_cached(url, host_policy)
            if key is not None:
                admitted_retries[key] = max(0, int(attempts))
        except (TypeError, ValueError):
            continue
    return admitted_retries


def _entry_depth(value: object, fallback: int) -> int:
    if isinstance(value, bool):
        return fallback
    if not isinstance(value, int | float | str):
        return fallback
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return fallback


def _entry_payload(entry: FrontierEntry) -> FrontierEntryDict:
    """The write edge: one record to its declared wire form. A non-entry in the
    frontier is a writer bug and dies here (AttributeError), never lands as a
    row the reader has to forgive."""
    return entry.to_dict()


def normalize_frontier(
    values: object,
    *,
    root_url: str | NormalizedUrl | None,
    policy: CrawlPolicy,
    host_policy: HostPolicy,
) -> tuple[list[FrontierEntry], list[str]]:
    """Coerce a persisted frontier's entry records into ``FrontierEntry``s.

    Canonicalize: coerce shape, keep the shallowest depth, drop the unusable.
    Returns the dropped records' raw urls too, so rows can be deleted."""
    if not isinstance(values, list):
        return [], []
    canonical_root = (
        root_url if isinstance(root_url, NormalizedUrl) else admit_or_none(root_url, policy=host_policy)
    )
    legacy_depth = policy.limits.max_depth
    entries: dict[NormalizedUrl, FrontierEntry] = {}
    order: list[NormalizedUrl] = []
    dropped: list[str] = []

    def _keep_shallowest(url: NormalizedUrl, entry_depth: int, raw_url: str) -> None:
        """First sighting keeps its slot; a later shallower one replaces it."""
        previous = entries.get(url)
        if previous is None:
            entries[url] = FrontierEntry(url=url, depth=entry_depth)
            order.append(url)
        elif entry_depth < previous.depth:
            entries[url] = FrontierEntry(url=url, depth=entry_depth)
        else:
            dropped.append(raw_url)

    for value in values:
        if not isinstance(value, dict):
            continue
        raw_url = value.get("url")
        depth = value.get("depth", legacy_depth)
        if not isinstance(raw_url, str) or not raw_url:
            continue
        url = _admit_cached(raw_url, host_policy)
        if url is None:
            continue
        entry_depth = 0 if url == canonical_root else _entry_depth(depth, legacy_depth)
        _keep_shallowest(url, entry_depth, raw_url)
    return [entries[url] for url in order], dropped


def normalize_visited(visited: list[str], *, host_policy: HostPolicy) -> OrderedUrlSet:
    """Coerce a persisted visited column to admitted values, keeping only what
    admits: text must pass admission, an admitted value keeps its identity.
    The collection is the insertion-ordered set; the load lands in one order.
    """
    normalized = OrderedUrlSet()
    for url in visited:
        if isinstance(url, str) and url:
            admitted = _admit_cached(url, host_policy)
            if admitted is not None:
                normalized.add(admitted)
    return normalized


__all__ = [
    "TERMINAL_STATUSES",
    "FrontierEntry",
    "JobKind",
    "JobStatus",
    "OrderedUrlSet",
    "job_max_pages",
    "normalize_frontier",
    "normalize_visited",
    "read_transient_retries",
]
