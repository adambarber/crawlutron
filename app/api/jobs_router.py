"""``/v1/jobs`` URL→handler mapping for whole-site crawl jobs.

Create spawns a background runner task (module :mod:`app.api.job_runner`)
that consumes the persisted frontier through the shared crawl engine.
State inspection (list/get) and row management (delete) remain direct
JobStore operations.
"""

from __future__ import annotations

from typing import NoReturn, cast

from fastapi import APIRouter

from app.api import job_runner
from app.api.dependencies import JobStoreDep, SettingsDep, ShutdownEventDep
from app.api.exceptions import http_error, validation_http_error
from app.api.main_helpers import _build_job_status
from app.api_models import (
    JobControlRequest,
    JobCreateRequest,
)
from app.crawl.job_store import JobStatusView
from app.crawl.payload_helpers import checked_payload_urls
from app.models.error_response import ErrorResponse
from app.models.host_policy import HostPolicy
from app.models.job_create_response import JobCreateResponse
from app.models.job_list_response import JobListResponse
from app.models.job_status_response import JobStatusResponse

router = APIRouter(prefix="/v1/jobs", tags=["Crawler"])


# ``NoReturn``, not ``None``: both of these always raise, and saying so is what
# lets the type checker narrow past their call sites. They were annotated ``None``,
# which is why three ``assert x is not None`` lines existed downstream to satisfy a
# checker that could not see the raise -- a suppression at the call site hiding a
# wrong signature at the definition.
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
        422: {"model": ErrorResponse, "description": "Invalid job request."},
    },
)
async def create_job(
    payload: JobCreateRequest,
    job_store: JobStoreDep,
    settings: SettingsDep,
    shutdown_event: ShutdownEventDep,
) -> JobCreateResponse:
    try:
        host_policy = HostPolicy.from_settings(settings)
        checked = checked_payload_urls(
            {"url": str(payload.root_url)},
            settings.max_batch_size,
            host_policy=host_policy,
        )
        job = await job_store.acreate(checked[0], payload.policy)
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
    description="Return a list of all crawl jobs with basic status info.",
)
async def list_jobs(job_store: JobStoreDep) -> JobListResponse:
    jobs = await job_store.alist_jobs()
    return JobListResponse(jobs=[_build_job_status(row) for row in jobs])


@router.get(
    "/{job_id}",
    summary="Get crawl job state and results",
    description=(
        "Return current status, frontier/visited/result counts, stats, and the "
        "persisted cap_truncated signal. Durable URL/depth entries remain internal."
    ),
    responses={404: {"model": ErrorResponse, "description": "Job not found."}},
)
async def get_job(job_id: str, job_store: JobStoreDep) -> JobStatusResponse:
    row = await job_store.astatus(job_id)
    if row is None:
        _raise_not_found(job_id)
    return _build_job_status(row)


# The same provider triple as ``create_job``, deliberately: creating a job and
# controlling one need identical state. The equality is verified against both
# live signatures (test_job_endpoints_declare_the_same_dependencies), not
# against a second hand-kept list of names here.
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
    # `aexists` above established the row, and the control path only reads it.
    row = cast("JobStatusView", row)
    return _build_job_status(row)


@router.delete(
    "/{job_id}",
    summary="Cancel (if running) and delete a crawl job",
    description="Permanently remove a crawl job and its data. Deleting a running job cancels its runner task "
    "first.",
    responses={404: {"model": ErrorResponse, "description": "Job not found."}},
)
async def delete_job(job_id: str, job_store: JobStoreDep) -> dict[str, str]:
    if job_runner.is_running(job_id):
        job_runner.cancel_job_task(job_id)
    if not await job_store.adelete(job_id):
        _raise_not_found(job_id)
    return {"status": "deleted", "job_id": job_id}


__all__ = ["router"]
