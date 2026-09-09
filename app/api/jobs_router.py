"""``/v1/jobs`` URL→handler mapping for whole-site crawl jobs (H5 make-it-work).

Create spawns a background runner task (module :mod:`app.api.job_runner`)
that consumes the persisted frontier through the shared crawl engine.
State inspection (list/get) and row management (delete) remain direct
JobStore operations.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.api import job_runner
from app.api.dependencies import get_job_store, get_settings, get_shutdown_event
from app.api.main_helpers import _build_job_status
from app.api_models import (
    CrawlerError,
    JobControlRequest,
    JobCreateRequest,
    JobCreateResponse,
    JobListResponse,
    JobStatusResponse,
)

router = APIRouter(prefix="/v1/jobs", tags=["Crawler"])


def _raise_not_found(job_id: str) -> None:
    raise HTTPException(
        status_code=404,
        detail={"code": "not_found", "message": f"Unknown job_id: {job_id}", "details": {}},
    )


def _raise_conflict(message: str) -> None:
    raise HTTPException(
        status_code=409,
        detail={"code": "invalid_state", "message": message, "details": {}},
    )


@router.post(
    "",
    summary="Create a whole-site crawl job and start executing it",
    description=(
        "Persist a resumable whole-site crawl job and immediately start a "
        "background runner that consumes the frontier through the shared "
        "crawl engine (same strategies and policy as POST /v1/crawl). "
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
    from app.crawl.job_store import JobStatus

    job = job_store.create(str(payload.root_url), payload.policy.model_dump())
    job_runner.start_job(job.job_id, job_store, settings, shutdown_event)
    job = job_store.load(job.job_id)
    return JobCreateResponse(
        job_id=job.job_id,
        status=job.status.value if job is not None else JobStatus.PENDING.value,
        root_url=job.root_url if job is not None else str(payload.root_url),
        created_at=job.created_at if job is not None else 0.0,
    )


@router.get(
    "",
    summary="List crawl jobs with live status",
    description="Return a list of all crawl jobs with basic status info.",
)
async def list_jobs(job_store=Depends(get_job_store)) -> JobListResponse:
    jobs = job_store.list_jobs()
    return JobListResponse(jobs=[JobStatusResponse(**j) for j in jobs])


@router.get(
    "/{job_id}",
    summary="Get crawl job state and results",
    description="Return the full job state including frontier, visited URLs, and results.",
    responses={404: {"model": CrawlerError, "description": "Job not found."}},
)
async def get_job(job_id: str, job_store=Depends(get_job_store)) -> JobStatusResponse:
    job = job_store.load(job_id)
    if job is None:
        _raise_not_found(job_id)
    return _build_job_status(job)


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
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_action", "message": f"Unknown action: {action}"},
        )
    if job_store.load(job_id) is None:
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
    job = job_store.load(job_id)
    return _build_job_status(job)


@router.delete(
    "/{job_id}",
    summary="Cancel (if running) and delete a crawl job",
    description="Permanently remove a crawl job and its data. Deleting a running job cancels its runner task first.",
    responses={404: {"model": CrawlerError, "description": "Job not found."}},
)
async def delete_job(job_id: str, job_store=Depends(get_job_store)) -> dict[str, str]:
    if job_runner.is_running(job_id):
        job_runner.cancel_job_task(job_id)
    if not job_store.delete(job_id):
        _raise_not_found(job_id)
    return {"status": "deleted", "job_id": job_id}


__all__ = ["router"]
