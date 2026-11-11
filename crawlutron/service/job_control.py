"""The one control-plane transition both job doors run: REST POST /control and MCP ``control_job``.

Spell the existence check, the runner dispatch, and the outcome mapping once;
a second copy is a second place a new action can miss the conflict mapping.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, NoReturn, cast

from crawlutron.api.exceptions import http_error
from crawlutron.models.job_status_response import JobStatusResponse
from crawlutron.service import job_runner
from crawlutron.service.main_helpers import build_job_status
from crawlutron.storage.job_store import JobStatusView

if TYPE_CHECKING:
    from crawlutron.models.caller_identity import CallerIdentity
    from crawlutron.storage.job_store import JobStore

_JobAction = Literal["pause", "resume", "cancel"]
"""The exhaustive control-action vocabulary both doors validate before dispatch."""


def raise_job_not_found(job_id: str) -> NoReturn:
    """The 404 both job doors answer for an unknown or foreign job_id."""
    raise http_error(404, "not_found", f"Unknown job_id: {job_id}")


def raise_job_conflict(message: str) -> NoReturn:
    raise http_error(409, "invalid_state", message)


async def control_job_core(
    job_id: str,
    action: _JobAction,
    job_store: JobStore,
    settings,
    shutdown_event,
    caller: CallerIdentity,
) -> JobStatusResponse:
    """Pause/resume/cancel one owned job and answer its fresh status view."""
    if not await job_store.aexists_for_caller(job_id, caller.caller_id):
        raise_job_not_found(job_id)

    match action:
        case "pause":
            outcome = await job_runner.pause_job(job_id, job_store)
        case "resume":
            outcome = await job_runner.resume_job(job_id, job_store, settings, shutdown_event)
        case "cancel":
            outcome = await job_runner.cancel_job(job_id, job_store)

    if outcome == "missing":
        raise_job_not_found(job_id)
    if outcome == "conflict":
        raise_job_conflict(f"Action '{action}' is not valid for the job's current state.")
    row = await job_store.astatus_for_caller(job_id, caller.caller_id)
    # `aexists` above established the row, and the control path only reads it.
    row = cast("JobStatusView", row)
    return build_job_status(row)


__all__ = ["control_job_core", "raise_job_conflict", "raise_job_not_found"]
