"""``/v1/jobs`` URL→handler mapping for whole-site crawl jobs (H5 make-it-work).

Create spawns a background runner task (module :mod:`app.api.job_runner`)
that consumes the persisted frontier through the shared crawl engine.
State inspection (list/get) and row management (delete) remain direct
JobStore operations.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api import job_runner
from app.api.dependencies import get_job_store, get_settings, get_shutdown_event
from app.api.exceptions import http_error
from app.api.main_helpers import _build_job_status
from app.api_models import (
    CrawlerError,
    JobControlRequest,
    JobCreateRequest,
    JobCreateResponse,
    JobListResponse,
    JobStatusResponse,
)
from app.crawl.payload_helpers import checked_payload_urls
from app.validation import HostPolicy

router = APIRouter(prefix="/v1/jobs", tags=["Crawler"])


def _raise_not_found(job_id: str) -> None:
    raise http_error(404, "not_found", f"Unknown job_id: {job_id}")


def _raise_conflict(message: str) -> None:
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
        422: {"model": CrawlerError, "description": "Invalid job request."},
    },
)
async def create_job(
    payload: JobCreateRequest,
    job_store=Depends(get_job_store),
    settings=Depends(get_settings),
    shutdown_event=Depends(get_shutdown_event),
) -> JobCreateResponse:
    try:
        host_policy = HostPolicy.from_settings(settings)
        checked = checked_payload_urls(
            {"url": str(payload.root_url)},
            settings.max_batch_size,
            host_policy=host_policy,
        )
        job = await job_store.acreate(checked[0], payload.policy.model_dump())
    except ValueError as error:
        raise http_error(422, "validation_error", "Request validation failed", {"errors": [{"msg": str(error)}]}) from error
    job_runner.start_job(job.job_id, job_store, settings, shutdown_event)
    row = await job_store.astatus(job.job_id)
    assert row is not None  # written moments ago by acreate
    return JobCreateResponse(
        job_id=row["job_id"],
        status=row["status"],
        root_url=row["root_url"],
        created_at=row["created_at"],
    )


@router.get(
    "",
    summary="List crawl jobs with live status",
    description="Return a list of all crawl jobs with basic status info.",
)
async def list_jobs(job_store=Depends(get_job_store)) -> JobListResponse:
    jobs = await job_store.alist_jobs()
    return JobListResponse(jobs=[_build_job_status(row) for row in jobs])


@router.get(
    "/{job_id}",
    summary="Get crawl job state and results",
    description=(
        "Return current status, frontier/visited/result counts, stats, and the "
        "persisted cap_truncated signal. Durable URL/depth entries remain internal."
    ),
    responses={404: {"model": CrawlerError, "description": "Job not found."}},
)
async def get_job(job_id: str, job_store=Depends(get_job_store)) -> JobStatusResponse:
    row = await job_store.astatus(job_id)
    if row is None:
        _raise_not_found(job_id)
    assert row is not None
    return _build_job_status(row)


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
        404: {"model": CrawlerError, "description": "Job not found."},
        409: {"model": CrawlerError, "description": "Action not valid for current job state."},
        422: {"model": CrawlerError, "description": "Invalid action."},
    },
)
async def control_job(
    job_id: str,
    payload: JobControlRequest,
    job_store=Depends(get_job_store),
    settings=Depends(get_settings),
    shutdown_event=Depends(get_shutdown_event),
) -> JobStatusResponse:
    action = payload.action.lower()
    if action not in {"pause", "resume", "cancel"}:
        raise http_error(422, "invalid_action", f"Unknown action: {action}")
    if not await job_store.aexists(job_id):
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
    row = await job_store.astatus(job_id)
    assert row is not None
    return _build_job_status(row)


@router.delete(
    "/{job_id}",
    summary="Cancel (if running) and delete a crawl job",
    description="Permanently remove a crawl job and its data. Deleting a running job cancels its runner task first.",
    responses={404: {"model": CrawlerError, "description": "Job not found."}},
)
async def delete_job(job_id: str, job_store=Depends(get_job_store)) -> dict[str, str]:
    if job_runner.is_running(job_id):
        job_runner.cancel_job_task(job_id)
    if not await job_store.adelete(job_id):
        _raise_not_found(job_id)
    return {"status": "deleted", "job_id": job_id}


__all__ = ["router"]
