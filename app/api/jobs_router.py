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

router = APIRouter(prefix="/v1/jobs", tags=["Crawler"])


def _raise_not_found(job_id: str) -> None:
    raise HTTPException(
        status_code=404,
        detail={"code": "not_found", "message": f"Unknown job_id: {job_id}", "details": {}},
    )


@router.post(
    "",
    summary="Job state persistence only — no engine executes whole-site jobs (H5)",
    description=(
        "Refuses with 501: this endpoint persists job rows, but no engine "
        "currently consumes the job frontier, so a created job would never "
        "crawl. Use POST /v1/crawl (or /v1/map) for actual crawling. "
        "Progression path: attach the crawl engine to the stored frontier "
        "and flip this endpoint to enqueue real work."
    ),
    responses={
        501: {"model": CrawlerError, "description": "Whole-site job execution is not implemented."},
        422: {"model": CrawlerError, "description": "Invalid job request."},
    },
)
async def create_job(
    payload: JobCreateRequest,
    job_store=Depends(get_job_store),
) -> JobCreateResponse:
    raise HTTPException(
        status_code=501,
        detail={
            "code": "jobs_not_executed",
            "message": (
                "Whole-site job execution is not implemented: no engine consumes "
                "the job frontier. Use /v1/crawl for crawling; job state persists "
                "but never executes."
            ),
            "details": {},
        },
    )


@router.get(
    "",
    summary="List crawl jobs (state inspection only — no engine executes jobs, H5)",
    description="Return a list of all persisted crawl job rows with basic status info. No engine currently consumes the job frontier, so statuses will not advance on their own.",
)
async def list_jobs(job_store=Depends(get_job_store)) -> JobListResponse:
    jobs = job_store.list_jobs()
    return JobListResponse(jobs=[JobStatusResponse(**j) for j in jobs])


@router.get(
    "/{job_id}",
    summary="Get crawl job state (state inspection only — no engine executes jobs, H5)",
    description="Return the full persisted job state including frontier, visited URLs, and results. No engine currently consumes the job frontier, so this reflects only rows written via the persistence API.",
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
    summary="Job control is refused — no engine runs jobs (H5)",
    description=(
        "Refuses with 501: pause/resume imply an engine executing the job, "
        "and none exists. Cancel is likewise execution-dependent. See the "
        "POST /v1/jobs description for the progression path."
    ),
    responses={
        501: {"model": CrawlerError, "description": "Job execution is not implemented."},
        422: {"model": CrawlerError, "description": "Invalid action."},
    },
)
async def control_job(
    job_id: str,
    payload: JobControlRequest,
    job_store=Depends(get_job_store),
) -> JobStatusResponse:
    action = payload.action.lower()
    if action not in {"pause", "resume", "cancel"}:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_action", "message": f"Unknown action: {action}"},
        )
    raise HTTPException(
        status_code=501,
        detail={
            "code": "jobs_not_executed",
            "message": (
                "Job control implies a running engine; whole-site job execution "
                "is not implemented. Use /v1/crawl for crawling."
            ),
            "details": {},
        },
    )


@router.delete(
    "/{job_id}",
    summary="Delete a persisted crawl job row",
    description="Permanently remove a persisted crawl job and its data (state management; no execution semantics).",
    responses={404: {"model": CrawlerError, "description": "Job not found."}},
)
async def delete_job(job_id: str, job_store=Depends(get_job_store)) -> dict[str, str]:
    if not job_store.delete(job_id):
        _raise_not_found(job_id)
    return {"status": "deleted", "job_id": job_id}


__all__ = ["router"]
