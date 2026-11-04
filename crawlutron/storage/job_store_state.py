"""The crawl-job record and the row's write spelling.

``_job_row_params`` is the only place the record flattens to the store's JSON
spelling; the read side (``job_store_read``) consumes that declared shape.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TypedDict

from crawlutron.admission.policy import PolicyTier, read_policy
from crawlutron.admission.policy import stored_policy as _stored_policy
from crawlutron.models.crawl_policy import CrawlPolicy
from crawlutron.models.host_policy import HostPolicy
from crawlutron.models.json_value import JsonValue
from crawlutron.models.normalized_url import NormalizedUrl, admit_or_none
from crawlutron.safety.json_safe import json_default

from .job_store_collections import (
    FrontierEntry,
    FrontierEntryDict,
    JobKind,
    JobStatus,
    OrderedUrlSet,
    _entry_payload,
    normalize_frontier,
    normalize_visited,
    read_transient_retries,
)

if TYPE_CHECKING:
    from .job_store import JobStore

_STATS_KEYS = ("total_crawled", "total_failed", "cap_truncated", "cap_overcommitted")


def _default_stats() -> dict[str, int]:
    return {key: 0 for key in _STATS_KEYS}


def _normalized_stats(value: object) -> dict[str, int]:
    stats = _default_stats()
    if isinstance(value, dict):
        for key in (*_STATS_KEYS, "total_discovered"):
            # ``total_discovered`` is reported only when the whole-site runner
            # counted admissions; a stored value is preserved, never seeded zero.
            if key == "total_discovered" and key not in value:
                continue
            raw = value.get(key, 0)
            if isinstance(raw, bool):
                continue
            try:
                stats[key] = int(raw)
            except (TypeError, ValueError):
                continue
    return stats


class CrawlJobRow(TypedDict):
    """The row one load reads, declared field by field.

    The inverse of ``_job_row_params``' write spelling: every ``json.loads``
    column lands here under its declared type, not as an undeclared guess.
    """

    job_id: str
    kind: str
    owner: str
    root_url: str
    policy: dict[str, JsonValue]
    status: str
    frontier: list[FrontierEntryDict]
    visited: list[str]
    results: list[dict[str, JsonValue]]
    created_at: float
    updated_at: float
    error: str | None
    stats: dict[str, int]
    in_flight: list[FrontierEntryDict]
    transient_retries: dict[str, int]


@dataclass
class CrawlJobState:
    """Persistent state of a crawl job (whole-site or single-shot)."""

    job_id: str
    kind: JobKind = JobKind.WHOLE_SITE
    root_url: str = ""
    owner: str = ""
    # The typed model, kept whole through the live job; only the write edge
    # (``_job_row_params``) flattens it to the store's JSON spelling.
    policy: CrawlPolicy = field(default_factory=CrawlPolicy)
    status: JobStatus = JobStatus.PENDING
    # Whole-site fields (used when kind == WHOLE_SITE)
    frontier: list[FrontierEntry] = field(default_factory=list)
    # The live-only frontier sync: ``frontier_mark`` is the persisted prefix and
    # ``frontier_removed`` names the rows the next persist deletes (loads re-derive both).
    frontier_mark: int = 0
    frontier_removed: list[NormalizedUrl] = field(default_factory=list)
    # Insertion-ordered set: the persist delta slices by position; only a
    # stable order makes that sound (see OrderedUrlSet).
    visited: OrderedUrlSet = field(default_factory=OrderedUrlSet)
    # High-water of what this process already sent: 0 on create, len on load
    # -- never the table's count, which a sibling's inserts make lie.
    visited_mark: int = 0
    in_flight: list[FrontierEntry] = field(default_factory=list)
    transient_retries: dict[NormalizedUrl, int] = field(default_factory=dict)  # admitted url -> retry count
    # Single-shot fields (kind == SINGLE_SHOT): root_url holds the first URL (or
    # "" for batch); results holds the response model's own dump per entry.
    results: list[dict[str, JsonValue]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    error: str | None = None
    stats: dict[str, int] = field(default_factory=_default_stats)
    host_policy: HostPolicy = field(default_factory=HostPolicy.unrestricted, repr=False, compare=False)
    # Deployment tier held on the row: a job outlives its request, and a stored
    # policy that omitted a limit must resolve as the operator's, not the default.
    tier: PolicyTier = field(default_factory=PolicyTier.model_defaults, repr=False, compare=False)

    @classmethod
    def from_json(cls, data: CrawlJobRow, *, host_policy: HostPolicy, tier: PolicyTier) -> CrawlJobState:
        """Build one live record from the declared row record.

        Collections normalize here; the cross-list identity re-derivation runs
        once per load at the read walk.
        """
        status = data.get("status", JobStatus.PENDING.value)
        kind = data.get("kind", JobKind.WHOLE_SITE.value)
        policy = read_policy(data["policy"], tier)
        root_url = str(data.get("root_url") or "")
        admitted_root = admit_or_none(root_url, policy=host_policy)
        root_url = admitted_root.raw if admitted_root is not None else ""
        # The queue's rows are url-keyed, so a duplicate identity in the stored
        # entries leaves a row the merged list no longer holds.
        frontier, deduped = normalize_frontier(
            data.get("frontier", []),
            root_url=root_url,
            policy=policy,
            host_policy=host_policy,
        )
        job = cls(
            job_id=str(data.get("job_id") or ""),
            kind=JobKind(kind),
            root_url=root_url,
            owner=str(data.get("owner") or ""),
            policy=policy,
            status=JobStatus(status),
            frontier=frontier,
            visited=normalize_visited(list(data.get("visited", [])), host_policy=host_policy),
            in_flight=normalize_frontier(
                data.get("in_flight", []),
                root_url=root_url,
                policy=policy,
                host_policy=host_policy,
            )[0],
            transient_retries=read_transient_retries(
                dict(data.get("transient_retries", {})), host_policy=host_policy
            ),
            results=list(data.get("results", [])),
            created_at=float(data.get("created_at", 0.0)),
            updated_at=float(data.get("updated_at", 0.0)),
            error=data.get("error") if isinstance(data.get("error"), str) else None,
            stats=_normalized_stats(data.get("stats", {})),
            host_policy=host_policy,
            tier=tier,
        )
        # A load's visited set IS the persisted set: this process starts its
        # delta at the end of it (a create starts at 0 -- the default).
        job.visited_mark = len(job.visited)
        # The queue is the same story: every loaded entry has its row,
        # except the identities the normalize deduped away.
        job.frontier_mark = len(job.frontier)
        job.frontier_removed.extend(NormalizedUrl(raw, policy=host_policy) for raw in deduped)
        return job


#: Late-save tombstone defense: a post-tombstone upsert is a no-op, so a racing
#: late ``save()`` cannot resurrect data columns on a tombstoned row.
_UPSERT_JOB_ROW_SQL = """
      INSERT INTO crawl_jobs (job_id, kind, root_url, policy, status,
                               created_at, updated_at, error, stats, in_flight, transient_retries,
                               owner)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(job_id) DO UPDATE SET
    kind=excluded.kind,
    root_url=excluded.root_url,
    policy=excluded.policy,
    updated_at=excluded.updated_at,
    error=excluded.error,
          stats=excluded.stats,
          in_flight=excluded.in_flight,
          transient_retries=excluded.transient_retries
WHERE deleted_at IS NULL
"""


def _json(value) -> str:
    return json.dumps(value, separators=(",", ":"), default=json_default)


def _retries_payload(retries: dict[NormalizedUrl, int]) -> dict[str, int]:
    """Wire keys for the retry counter: the persisted column is text-keyed."""
    return {url.raw: attempts for url, attempts in retries.items()}


def _job_row_params(job: CrawlJobState) -> tuple[object, ...]:
    """The live-state columns of the job row, in ``_UPSERT_JOB_ROW_SQL`` order.
    Separate from the statement so the two cannot drift: a column added to the
    SQL without a matching entry here is a TypeError at first persist, not a silent drop.
    """
    return (
        job.job_id,
        job.kind.value,
        job.root_url,
        _json(_stored_policy(job.policy)),
        job.status.value,
        job.created_at,
        job.updated_at,
        job.error,
        _json(job.stats),
        _json([_entry_payload(entry) for entry in job.in_flight]),
        _json(_retries_payload(job.transient_retries)),
        job.owner,
    )


def _create_job(
    store: JobStore,
    job_id: str,
    kind: JobKind,
    root_url: str,
    policy: CrawlPolicy,
    frontier: list[FrontierEntry] | None = None,
    *,
    owner: str = "",
) -> CrawlJobState:
    """The one place a job is born, for both kinds, and the one place it is written.
    A module function so the row's content and the locked write are one decision;
    the two kinds used to split across that boundary and drift (one lost its tier).
    """
    job = CrawlJobState(
        job_id=job_id,
        kind=kind,
        root_url=root_url,
        owner=owner,
        policy=policy,
        status=JobStatus.PENDING,
        frontier=frontier or [],
        host_policy=store._host_policy,
        tier=store.tier,
    )
    with store._lock:
        store._persist(job)
    return job


__all__ = [
    "CrawlJobState",
]


_CAS_STATUS_SQL = """
      -- json_each keeps the expected set a BOUND parameter; COALESCE keeps the
      -- stored error on an error-less CAS (the stamp rides the CAS).
      UPDATE crawl_jobs SET status = ?, updated_at = ?,
                             error = COALESCE(?, error)
      WHERE job_id = ? AND deleted_at IS NULL
        AND status IN (SELECT value FROM json_each(?))
"""
