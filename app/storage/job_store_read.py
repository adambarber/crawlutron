"""The declared reads off the crawl-jobs row: the row record's walk into
``CrawlJobState``, the live-state merge, and the status projection.

Every read here consumes the declared ``CrawlJobRow`` shape.
"""

from __future__ import annotations

import json
import sqlite3
from typing import TypedDict

from app.admission.policy import PolicyTier, read_policy
from app.models.host_policy import HostPolicy
from app.models.json_value import JsonValue
from app.models.normalized_url import NormalizedUrl, admit_or_none

from .job_store_collections import FrontierEntry, FrontierEntryDict
from .job_store_state import CrawlJobRow, CrawlJobState, _normalized_stats


def row_to_state(
    row: sqlite3.Row,
    *,
    visited: list[str],
    results: list[dict[str, JsonValue]],
    host_policy: HostPolicy,
    tier: PolicyTier,
) -> CrawlJobState:
    """One row, read through the declared record: every ``json.loads`` column
    lands in ``CrawlJobRow`` under its declared type -- the read no longer
    guesses the shape it wrote."""
    job_id = row["job_id"]
    policy: dict[str, JsonValue] = json.loads(row["policy"])
    frontier: list[FrontierEntryDict] = json.loads(row["frontier"])
    stats: dict[str, int] = json.loads(row["stats"]) if row["stats"] else {}
    in_flight: list[FrontierEntryDict] = json.loads(row["in_flight"])
    transient_retries: dict[str, int] = json.loads(row["transient_retries"])
    job = CrawlJobState.from_json(
        {
            "job_id": job_id,
            "kind": row["kind"],
            "owner": row["owner"],
            "root_url": row["root_url"],
            "policy": policy,
            "status": row["status"],
            "frontier": frontier,
            "visited": visited,
            "results": results,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "error": row["error"],
            "stats": stats,
            "in_flight": in_flight,
            "transient_retries": transient_retries,
        },
        host_policy=host_policy,
        tier=tier,
    )
    # The read establishes identity; re-derive it over the assembled collections
    # once per load, so no writer can leave a row reading as two.
    return normalize_job_state(job)


def normalize_job_state(job: CrawlJobState) -> CrawlJobState:
    """Re-derive one live job's identity over its collections.

    The collections are records the process built, so this is a merge, not a
    shape coercion -- the two no longer share a shape-forgiving path."""
    admitted_root = admit_or_none(job.root_url, policy=job.host_policy)
    root_url = admitted_root.raw if admitted_root is not None else ""
    policy = read_policy(job.policy, job.tier)
    visited, frontier, in_flight = _merge_live_collections(
        root_url=root_url,
        host_policy=job.host_policy,
        visited=job.visited,
        frontier=job.frontier,
        in_flight=job.in_flight,
    )
    transient_retries: dict[NormalizedUrl, int] = {}
    for url, attempts in job.transient_retries.items():
        try:
            value = max(0, int(attempts))
        except (TypeError, ValueError):
            continue
        transient_retries[url] = max(transient_retries.get(url, 0), value)
    job.root_url = root_url
    job.policy = policy
    job.visited = visited
    job.frontier = frontier
    job.in_flight = in_flight
    job.transient_retries = transient_retries
    job.stats = _normalized_stats(job.stats)
    return job


def _merge_live_collections(
    *,
    root_url: str,
    host_policy: HostPolicy,
    visited: set[NormalizedUrl],
    frontier: list[FrontierEntry],
    in_flight: list[FrontierEntry],
) -> tuple[set[NormalizedUrl], list[FrontierEntry], list[FrontierEntry]]:
    """The live merge the row read's normalization once duplicated: the root folds
    to depth 0, visited wins, in_flight folds to the frontier's shallowest depth,
    over records, never over forgiven wire shapes."""
    canonical_root = admit_or_none(root_url, policy=host_policy)
    folded_frontier = [
        entry
        if not (canonical_root is not None and entry.url == canonical_root)
        else FrontierEntry(url=entry.url, depth=0)
        for entry in frontier
    ]
    frontier_by_url = {entry.url: entry for entry in folded_frontier}
    folded_in_flight = [
        FrontierEntry(
            url=entry.url,
            depth=min(entry.depth, frontier_by_url[entry.url].depth)
            if entry.url in frontier_by_url
            else entry.depth,
        )
        for entry in in_flight
        if entry.url not in visited
    ]
    in_flight_ids = {entry.url for entry in folded_in_flight}
    kept_frontier = [
        entry for entry in folded_frontier if entry.url not in visited and entry.url not in in_flight_ids
    ]
    return visited, kept_frontier, folded_in_flight


# Every field the job response exposes, shared by single and list reads so the
# two cannot drift into describing different jobs.
_JOB_STATUS_PROJECTION = """
    SELECT job_id, root_url, status, created_at, updated_at, stats, error, owner,
           COALESCE(kind, 'whole_site') AS kind,
           json_array_length(frontier) AS frontier_size,
           COALESCE(json_extract(stats, '$.cap_truncated'), 0) AS cap_truncated
      FROM crawl_jobs
"""

# The list order: newest first, with the job id breaking a tie, so a page of
# two rows created in the same instant has one order and not two.
_JOB_LIST_ORDER = " ORDER BY created_at DESC, job_id DESC"


class JobStatusView(TypedDict):
    """The status view: one record, one declared shape, every key mandatory.
    A TypedDict so the compiler catches a view/response-model key mismatch (the
    old dict[str, Any] dropped kind silently, which recovery read as whole-site).
    """

    job_id: str
    root_url: str
    owner: str
    status: str
    kind: str
    created_at: float
    updated_at: float
    frontier_size: int
    visited_size: int
    results_count: int
    cap_truncated: bool
    stats: dict[str, int]
    error: str | None


def _status_row(row: sqlite3.Row, visited: int, results: int) -> JobStatusView:
    """Shape one projected row into the status view the API returns: frontier_size
    and cap_truncated are NULL on a fresh job, and ``kind`` is selected because
    recovery reads it from this view (it was missing, so every job read as whole-site).
    """
    return {
        "job_id": row["job_id"],
        "root_url": row["root_url"],
        "owner": row["owner"],
        "status": row["status"],
        "kind": row["kind"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "frontier_size": row["frontier_size"] or 0,
        "visited_size": visited,
        "results_count": results,
        "cap_truncated": bool(row["cap_truncated"]),
        # The stats column was written from ``dict[str, int]``; the read is the
        # checked one, so a corrupt column reads as zeros, never as the raw value.
        "stats": _normalized_stats(json.loads(row["stats"])) if row["stats"] else {},
        "error": row["error"],
    }


__all__ = [
    "CrawlJobRow",
    "JobStatusView",
    "normalize_job_state",
    "row_to_state",
]
