"""``/v1/jobs`` URL→handler mapping for whole-site crawl jobs.

Create/list/get/control/delete delegate to the shared typed job cores
(:mod:`crawlutron.api.job_cores`); this module is only the URL→handler mapping.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Query

from crawlutron.api.caller_auth import (
    quota_denials,
)
from crawlutron.api.dependencies import CallerDep, JobStoreDep, SettingsDep, ShutdownEventDep, TrackingDep
from crawlutron.api.exceptions import validation_http_error
from crawlutron.api.route_layer import guarded_router
from crawlutron.models.error_response import ErrorResponse
from crawlutron.models.job_control_request import JobControlRequest
from crawlutron.models.job_create_request import JobCreateRequest
from crawlutron.models.job_create_response import JobCreateResponse
from crawlutron.models.job_deleted_response import JobDeletedResponse
from crawlutron.models.job_list_response import JobListResponse
from crawlutron.models.job_status_response import JobStatusResponse
from crawlutron.service import job_runner
from crawlutron.service.job_control import control_job_core, raise_job_not_found
from crawlutron.service.job_cores import create_job_core
from crawlutron.service.main_helpers import build_job_status
from crawlutron.vocabulary.limits import DEFAULT_JOB_LIST_LIMIT, MAX_JOB_LIST_LIMIT

router = guarded_router(prefix="/v1/jobs", tags=["Crawler"])


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
    tracking: TrackingDep,
) -> JobCreateResponse:
    try:
        return await create_job_core(
            payload,
            job_store,
            settings,
            shutdown_event,
            caller,
            concurrency_tracker=tracking.concurrency_tracker,
        )
    except ValueError as error:
        raise validation_http_error(error) from error


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
        raise_job_not_found(job_id)
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
    action = payload.action
    return await control_job_core(job_id, action, job_store, settings, shutdown_event, caller)


@router.delete(
    "/{job_id}",
    summary="Cancel (if running) and delete a crawl job",
    description="Permanently remove a crawl job and its data. Deleting a running job cancels its runner task "
    "first.",
    responses={
        404: {"model": ErrorResponse, "description": "Job not found."},
    },
)
async def delete_job(job_id: str, job_store: JobStoreDep, caller: CallerDep) -> JobDeletedResponse:
    if not await job_store.aexists_for_caller(job_id, caller.caller_id):
        raise_job_not_found(job_id)
    if job_runner.is_running(job_id):
        job_runner.cancel_job_task(job_id)
    if not await job_store.adelete_for_caller(job_id, caller.caller_id):
        raise_job_not_found(job_id)
    return JobDeletedResponse(deleted=True, job_id=job_id)


__all__ = ["router"]
