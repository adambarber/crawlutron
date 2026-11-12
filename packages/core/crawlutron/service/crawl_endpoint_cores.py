"""The crawl-read CORES: the delta loader and the two MCP read tools.

Zero Request coupling; the 404 miss raises the typed :class:`RequestNotFound`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from crawlutron.delta import (
    BaselineEntry,
    BaselineEvidence,
    DeltaContext,
    extract_baseline,
    extract_baseline_entries,
)
from crawlutron.models.crawl_pending import CrawlPendingError
from crawlutron.models.crawl_request import CrawlRequest
from crawlutron.models.json_value import JsonValue
from crawlutron.models.trace_response import TraceResponse
from crawlutron.service.errors import RequestNotFound
from crawlutron.storage.job_store import TERMINAL_STATUSES, JobKind

if TYPE_CHECKING:
    from crawlutron.storage.job_store import JobStore
    from crawlutron.storage.result_store import CrawlResultStore


async def delta_context_for(
    payload: CrawlRequest, caller_id: str, result_store: CrawlResultStore | None
) -> DeltaContext | None:
    """Caller-scoped delta request for ``payload``, or ``None`` when no delta knob
    was set. With ``baseline_request_id`` set, the caller-isolated baseline loads
    and degrades (missing/expired/foreign rows surface as ``baseline_expired``)."""
    if payload.changed_since is None and payload.baseline_request_id is None:
        return None
    baseline_request_id = payload.baseline_request_id
    baseline: dict[str, BaselineEvidence] = {}
    entries: tuple[BaselineEntry, ...] = ()
    baseline_expired = False
    if baseline_request_id is not None:
        stored = (
            None
            if result_store is None
            else await result_store.aget_for_caller(baseline_request_id, caller_id)
        )
        if stored is None:
            baseline_expired = True
        elif isinstance(stored, dict):
            baseline = extract_baseline(cast("dict[str, object]", stored))
            entries = extract_baseline_entries(cast("dict[str, object]", stored))
        else:
            baseline_expired = True
    return DeltaContext(
        changed_since=payload.changed_since,
        baseline_request_id=baseline_request_id,
        baseline=baseline,
        baseline_expired=baseline_expired,
        entries=entries,
    )


async def stored_response_payload(
    request_id: str,
    job_store: JobStore,
    result_store: CrawlResultStore | None,
    caller_id: str = "",
) -> JsonValue | None:
    """The stored response for ``request_id``, or None, shared by both replay
    doors: a job not in a terminal state raises 202; the sync path with no job
    answers from the store. Reads scope to ``caller_id``; foreign rows miss.
    """
    job = await job_store.aload_for_caller(request_id, caller_id)
    if job is not None and job.status not in TERMINAL_STATUSES:
        # Typed, not an HTTP status: REST converts it to the 202 below, and the
        # MCP read door answers the running state as a poll-again result.
        raise CrawlPendingError(request_id, job.status.value)
    # The MCP read door passes no store when the app has none wired; the job
    # row below is then the only source, which is the durable copy anyway.
    stored = None if result_store is None else await result_store.aget_for_caller(request_id, caller_id)
    if stored is not None:
        return stored
    if job is not None and job.kind == JobKind.SINGLE_SHOT and job.results:
        # Same wire payload, written by the runner to the job's results. It is
        # also the durable copy: the result cache expires, the job row does not.
        return job.results[0]
    return None


async def read_crawl_result_record(
    request_id: str,
    job_store: JobStore,
    result_store: CrawlResultStore | None,
    caller_id: str = "",
) -> JsonValue:
    """The stored crawl response for the MCP read tool, as stored.

    Reads the same rows the replay door serves, without re-admitting URL fields.
    A running job raises the typed :class:`CrawlPendingError` (non-error door).
    """
    payload = await stored_response_payload(request_id, job_store, result_store, caller_id)
    if payload is None:
        raise RequestNotFound(request_id)
    return payload


async def read_crawl_trace_record(
    request_id: str,
    job_store: JobStore,
    result_store: CrawlResultStore | None,
    caller_id: str = "",
) -> TraceResponse:
    """The stored record read as a trace, for the MCP read tool."""
    payload = await read_crawl_result_record(request_id, job_store, result_store, caller_id)
    return TraceResponse.model_validate(payload)


__all__ = [
    "delta_context_for",
    "read_crawl_result_record",
    "read_crawl_trace_record",
    "stored_response_payload",
]
