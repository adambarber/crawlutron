"""``/v1/jobs`` URL→handler mapping for whole-site crawl jobs.

Create spawns a background runner (:mod:`app.api.job_runner`) consuming the persisted
frontier; list/get/delete stay direct JobStore operations.
"""

from __future__ import annotations

from typing import Annotated, NoReturn, cast

from fastapi import APIRouter, Query, Request

from app.admission.payload_helpers import checked_payload_urls
from app.admission.policy import read_policy
from app.api import job_runner
from app.api.caller_auth import (
    Tracking,
    admit,
    auth_denials,
    effective_cookie_namespace,
    quota_denials,
    resolve_page_limit,
)
from app.api.dependencies import CallerDep, JobStoreDep, SettingsDep, ShutdownEventDep
from app.api.exceptions import http_error, validation_http_error
from app.api.main_helpers import build_job_status
from app.models.crawl_request import CrawlRequest
from app.models.error_response import ErrorResponse
from app.models.host_policy import HostPolicy
from app.models.job_control_request import JobControlRequest
from app.models.job_create_request import JobCreateRequest
from app.models.job_create_response import JobCreateResponse
from app.models.job_list_response import JobListResponse
from app.models.job_status_response import JobStatusResponse
from app.storage.job_store import JobStatusView
from app.transport.request_profile import cookie_namespace_for
from app.vocabulary.limits import DEFAULT_JOB_LIST_LIMIT, MAX_JOB_LIST_LIMIT

router = APIRouter(prefix="/v1/jobs", tags=["Crawler"], responses=auth_denials())


# ``NoReturn``, not ``None``: both of these always raise, and saying so lets the
# checker narrow past the call site instead of demanding a downstream assert.
def _raise_not_found(job_id: str) -> NoReturn:
    raise http_error(404, "not_found", f"Unknown job_id: {job_id}")


def _raise_conflict(message: str) -> NoReturn:
    raise http_error(409, "invalid_state", message)


@router.post(
    "",
    summary="Create a whole-site crawl job and start executing it",
    description=(
        "Persist a resumable whole-site crawl job and immediately start a "
        "background runner. The durable runner owns depth-aware canonical "
        "frontier admission (scope, documents, analytics, robots, and max_pages), "
        "then uses the shared crawl engine for non-recursive page batches. "
        "Control the job via POST /v1/jobs/{job_id}/control."
    ),
    responses={
        **quota_denials(),
        422: {"model": ErrorResponse, "description": "Invalid job request."},
    },
)
async def create_job(
    payload: JobCreateRequest,
    job_store: JobStoreDep,
    settings: SettingsDep,
    shutdown_event: ShutdownEventDep,
    caller: CallerDep,
    request: Request,
) -> JobCreateResponse:
    try:
        host_policy = HostPolicy.from_settings(settings)
        checked = checked_payload_urls(
            CrawlRequest(url=str(payload.root_url)),
            settings.max_batch_size,
            host_policy=host_policy,
        )
        resolved_policy = read_policy(payload.policy, job_store.tier)
        # #194: resolve the page bound for every caller (keyed and anonymous) and
        # write it back so the durable row records the bound, not an unbounded 0.
        bound = resolve_page_limit(caller, resolved_policy.limits.max_pages)
        resolved_policy = resolved_policy.model_copy(
            update={"limits": resolved_policy.limits.model_copy(update={"max_pages": bound})}
        )
        # #195/#226: persist the caller's effective cookie namespace so a keyed whole-site job
        # crawls under its own jar partition; the durable row outlives the admitting request.
        namespace = effective_cookie_namespace(resolved_policy.request_profile, caller)
        # #537: a refused key id must not survive in the durable row for the engine to re-derive.
        if namespace or namespace != cookie_namespace_for(resolved_policy.request_profile):
            profile = dict(resolved_policy.request_profile)
            profile["cookie_namespace"] = namespace
            resolved_policy = resolved_policy.model_copy(update={"request_profile": profile})
        # A whole-site job persists a durable row; admit reserves a slot across the
        # check->insert window and hands it to the durable row at block exit.
        tracking = Tracking(
            job_store=job_store, concurrency_tracker=getattr(request.app.state, "concurrency_tracker", None)
        )
        async with admit(caller, bound, tracking=tracking, reserve_slot=False):
            job = await job_store.acreate(checked[0][0], resolved_policy, owner=caller.caller_id)
    except ValueError as error:
        raise validation_http_error(error) from error
    job_runner.start_job(job.job_id, job_store, settings, shutdown_event)
    row = await job_store.astatus(job.job_id)
    # Non-None by construction -- `acreate` wrote it moments ago on the same
    # store -- so this narrows for the type checker rather than guarding.
    row = cast("JobStatusView", row)
    return JobCreateResponse(
        job_id=row["job_id"],
        status=row["status"],
        root_url=row["root_url"],
        created_at=row["created_at"],
    )


@router.get(
    "",
    summary="List crawl jobs with live status",
    description=(
        "Return a page of the caller's crawl jobs with basic status info, newest "
        "first. The page is bounded by ``limit`` (default "
        f"{DEFAULT_JOB_LIST_LIMIT}, capped at {MAX_JOB_LIST_LIMIT}); ``offset`` "
        "skips that many rows. ``total`` reports the caller's full row count across "
        "every page."
    ),
    responses={
        422: {"model": ErrorResponse, "description": "Invalid limit/offset."},
    },
)
async def list_jobs(
    job_store: JobStoreDep,
    caller: CallerDep,
    limit: Annotated[int | None, Query(ge=1, le=MAX_JOB_LIST_LIMIT)] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> JobListResponse:
    # FastAPI's Query le/ge bound the page; the store's SQL slice then
    # spares a caller with thousands of rows from re-materializing all of them.
    page_limit = limit if limit is not None else DEFAULT_JOB_LIST_LIMIT
    jobs = await job_store.alist_for_caller(caller.caller_id, limit=page_limit, offset=offset)
    total = await job_store.acount_for_caller(caller.caller_id)
    return JobListResponse(jobs=[build_job_status(row) for row in jobs], total=total)


@router.get(
    "/{job_id}",
    summary="Get crawl job state and results",
    description=(
        "Return current status, frontier/visited/result counts, stats, and the "
        "persisted cap_truncated signal. Durable URL/depth entries remain internal."
    ),
    responses={
        404: {"model": ErrorResponse, "description": "Job not found."},
    },
)
async def get_job(job_id: str, job_store: JobStoreDep, caller: CallerDep) -> JobStatusResponse:
    row = await job_store.astatus_for_caller(job_id, caller.caller_id)
    if row is None:
        _raise_not_found(job_id)
    return build_job_status(row)


# The same provider triple as create_job, deliberately: the equality is verified
# against both live signatures by the endpoints test, not a hand-kept list here.
@router.post(
    "/{job_id}/control",
    summary="Control a running crawl job (pause/resume/cancel)",
    description=(
        "Pause (runner exits at the next batch boundary, frontier retained), "
        "resume (runner task respawns from the persisted frontier), or cancel "
        "(terminal state; frontier retained for audit). Deleting a running "
        "job also cancels it."
    ),
    responses={
        404: {"model": ErrorResponse, "description": "Job not found."},
        409: {"model": ErrorResponse, "description": "Action not valid for current job state."},
        422: {"model": ErrorResponse, "description": "Invalid action."},
    },
)
async def control_job(
    job_id: str,
    payload: JobControlRequest,
    job_store: JobStoreDep,
    settings: SettingsDep,
    shutdown_event: ShutdownEventDep,
    caller: CallerDep,
) -> JobStatusResponse:
    action = payload.action.lower()
    if action not in {"pause", "resume", "cancel"}:
        raise http_error(422, "invalid_action", f"Unknown action: {action}")
    if not await job_store.aexists_for_caller(job_id, caller.caller_id):
        _raise_not_found(job_id)

    if action == "pause":
        outcome = await job_runner.pause_job(job_id, job_store)
    elif action == "resume":
        outcome = await job_runner.resume_job(job_id, job_store, settings, shutdown_event)
    else:
        outcome = await job_runner.cancel_job(job_id, job_store)

    if outcome == "missing":
        _raise_not_found(job_id)
    if outcome == "conflict":
        _raise_conflict(f"Action '{action}' is not valid for the job's current state.")
    row = await job_store.astatus_for_caller(job_id, caller.caller_id)
    # `aexists` above established the row, and the control path only reads it.
    row = cast("JobStatusView", row)
    return build_job_status(row)


@router.delete(
    "/{job_id}",
    summary="Cancel (if running) and delete a crawl job",
    description="Permanently remove a crawl job and its data. Deleting a running job cancels its runner task "
    "first.",
    responses={
        404: {"model": ErrorResponse, "description": "Job not found."},
    },
)
async def delete_job(job_id: str, job_store: JobStoreDep, caller: CallerDep) -> dict[str, str]:
    if not await job_store.aexists_for_caller(job_id, caller.caller_id):
        _raise_not_found(job_id)
    if job_runner.is_running(job_id):
        job_runner.cancel_job_task(job_id)
    if not await job_store.adelete_for_caller(job_id, caller.caller_id):
        _raise_not_found(job_id)
    return {"status": "deleted", "job_id": job_id}


__all__ = ["router"]
