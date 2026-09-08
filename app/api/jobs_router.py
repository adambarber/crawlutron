"""``/v1/jobs`` URL→handler mapping for whole-site crawl jobs.

The handlers are inline (small enough not to warrant their own module).
Each handler delegates to the shared ``JobStore`` instance injected via
``app.state`` and the :func:`get_job_store` FastAPI dependency.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.api.dependencies import get_job_store
from app.api_models import (
    CrawlerError,
    JobControlRequest,
    JobCreateRequest,
    JobCreateResponse,
    JobListResponse,
    JobStatusResponse,
)
from app.crawl.job_store import JobStatus

router = APIRouter(prefix="/v1/jobs", tags=["Crawler"])


def _raise_not_found(job_id: str) -> None:
    raise HTTPException(
        status_code=404,
        detail={"code": "not_found", "message": f"Unknown job_id: {job_id}", "details": {}},
    )


@router.post(
    "",
    summary="Create a new resumable whole-site crawl job (#3+#14)",
    description=(
        "Create a crawl job that recursively discovers and crawls URLs from a root URL. "
        "The job persists its state (frontier, visited, results) to disk and can be "
        "paused, resumed, and inspected across restarts. Callers own the job lifecycle."
    ),
    responses={422: {"model": CrawlerError, "description": "Invalid job request."}},
)
async def create_job(
    payload: JobCreateRequest,
    job_store=Depends(get_job_store),
) -> JobCreateResponse:
    job = job_store.create(payload.root_url, payload.policy.model_dump(exclude_none=True))
    return JobCreateResponse(
        job_id=job.job_id,
        status=job.status.value,
        root_url=job.root_url,
        created_at=job.created_at,
    )


@router.get(
    "",
    summary="List all crawl jobs (#3+#14)",
    description="Return a list of all crawl jobs with basic status info.",
)
async def list_jobs(job_store=Depends(get_job_store)) -> JobListResponse:
    jobs = job_store.list_jobs()
    return JobListResponse(jobs=[JobStatusResponse(**j) for j in jobs])


@router.get(
    "/{job_id}",
    summary="Get crawl job status and results (#3+#14)",
    description="Return the full job state including frontier, visited URLs, and results.",
    responses={404: {"model": CrawlerError, "description": "Job not found."}},
)
async def get_job(job_id: str, job_store=Depends(get_job_store)) -> JobStatusResponse:
    from app.api.main_helpers import _build_job_status

    job = job_store.load(job_id)
    if job is None:
        _raise_not_found(job_id)
    return _build_job_status(job)


@router.post(
    "/{job_id}/control",
    summary="Control a crawl job (pause/resume/cancel) (#3+#14)",
    description="Pause, resume, or cancel a running crawl job.",
    responses={
        404: {"model": CrawlerError, "description": "Job not found."},
        422: {"model": CrawlerError, "description": "Invalid action."},
    },
)
async def control_job(
    job_id: str,
    payload: JobControlRequest,
    job_store=Depends(get_job_store),
) -> JobStatusResponse:
    from app.api.main_helpers import _build_job_status

    job = job_store.load(job_id)
    if job is None:
        _raise_not_found(job_id)

    action = payload.action.lower()
    if action == "pause":
        if job.status != JobStatus.RUNNING:
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid_state", "message": "Can only pause a running job"},
            )
        job.status = JobStatus.PAUSED
    elif action == "resume":
        if job.status != JobStatus.PAUSED:
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid_state", "message": "Can only resume a paused job"},
            )
        job.status = JobStatus.RUNNING
    elif action == "cancel":
        if job.status in {JobStatus.COMPLETED, JobStatus.CANCELLED, JobStatus.FAILED}:
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid_state", "message": "Job already terminal"},
            )
        job.status = JobStatus.CANCELLED
    else:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_action", "message": f"Unknown action: {action}"},
        )

    job_store.save(job)
    return _build_job_status(job)


@router.delete(
    "/{job_id}",
    summary="Delete a crawl job (#3+#14)",
    description="Permanently remove a crawl job and its data.",
    responses={404: {"model": CrawlerError, "description": "Job not found."}},
)
async def delete_job(job_id: str, job_store=Depends(get_job_store)) -> dict[str, str]:
    if not job_store.delete(job_id):
        _raise_not_found(job_id)
    return {"status": "deleted", "job_id": job_id}


__all__ = ["router"]
