"""The typed job cores both doors run: REST ``/v1/jobs`` and the MCP job tools.

Every caller-facing job operation is spelled once here -- create, list, get,
control, delete -- so caller scoping and quota admission cannot diverge
between the HTTP door and the MCP tool.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from crawlutron.admission.payload_helpers import checked_payload_urls
from crawlutron.admission.policy import read_policy
from crawlutron.api import job_runner
from crawlutron.api.caller_auth import (
    Tracking,
    admit,
    effective_cookie_namespace,
    refuse_anonymous_cookie_namespace,
    resolve_page_limit,
)
from crawlutron.api.job_control import control_job_core, raise_job_not_found
from crawlutron.api.main_helpers import build_job_status
from crawlutron.models.crawl_request import CrawlRequest
from crawlutron.models.host_policy import HostPolicy
from crawlutron.models.job_control_tool_request import JobControlToolRequest
from crawlutron.models.job_create_response import JobCreateResponse
from crawlutron.models.job_deleted_response import JobDeletedResponse
from crawlutron.models.job_list_response import JobListResponse
from crawlutron.transport.request_profile import cookie_namespace_for
from crawlutron.vocabulary.limits import DEFAULT_JOB_LIST_LIMIT

if TYPE_CHECKING:
    from crawlutron.config.settings import CrawlerSettings
    from crawlutron.models.caller_identity import CallerIdentity
    from crawlutron.models.job_create_request import JobCreateRequest
    from crawlutron.models.job_id_request import JobIdRequest
    from crawlutron.models.job_list_request import JobListRequest
    from crawlutron.models.job_status_response import JobStatusResponse
    from crawlutron.storage.job_store import JobStatusView, JobStore


async def create_job_core(
    payload: JobCreateRequest,
    job_store: JobStore,
    settings: CrawlerSettings,
    shutdown_event,
    caller: CallerIdentity,
    *,
    concurrency_tracker=None,
) -> JobCreateResponse:
    """Persist one whole-site crawl job for ``caller`` and start its runner."""
    host_policy = HostPolicy.from_settings(settings)
    checked = checked_payload_urls(
        CrawlRequest(url=str(payload.root_url)),
        settings.max_batch_size,
        host_policy=host_policy,
    )
    resolved_policy = read_policy(payload.policy, job_store.tier)
    # Resolve the page bound for every caller (keyed and anonymous) and write it
    # back so the durable row records the bound, not an unbounded 0.
    bound = resolve_page_limit(caller, resolved_policy.limits.max_pages)
    resolved_policy = resolved_policy.model_copy(
        update={"limits": resolved_policy.limits.model_copy(update={"max_pages": bound})}
    )
    # Persist the caller's effective cookie namespace so a keyed whole-site job
    # crawls under its own jar partition; an anonymous declaration is refused first.
    refuse_anonymous_cookie_namespace(caller, resolved_policy.request_profile)
    namespace = effective_cookie_namespace(resolved_policy.request_profile, caller)
    # A refused key id must not survive in the durable row for the engine to re-derive.
    if namespace or namespace != cookie_namespace_for(resolved_policy.request_profile):
        profile = dict(resolved_policy.request_profile)
        profile["cookie_namespace"] = namespace
        resolved_policy = resolved_policy.model_copy(update={"request_profile": profile})
    # A whole-site job persists a durable row; admit reserves a slot across the
    # check->insert window and hands it to the durable row at block exit.
    tracking = Tracking(job_store=job_store, concurrency_tracker=concurrency_tracker)
    async with admit(caller, bound, tracking=tracking, reserve_slot=False):
        job = await job_store.acreate(checked[0][0], resolved_policy, owner=caller.caller_id)
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


async def list_jobs_core(
    payload: JobListRequest, job_store, settings, shutdown_event, caller: CallerIdentity
) -> JobListResponse:
    """The owned, newest-first page GET /v1/jobs answers."""
    caller_id = caller.caller_id
    page_limit = payload.limit if payload.limit is not None else DEFAULT_JOB_LIST_LIMIT
    jobs = await job_store.alist_for_caller(caller_id, limit=page_limit, offset=payload.offset)
    total = await job_store.acount_for_caller(caller_id)
    return JobListResponse(jobs=[build_job_status(row) for row in jobs], total=total)


async def get_job_core(
    payload: JobIdRequest, job_store, settings, shutdown_event, caller: CallerIdentity
) -> JobStatusResponse:
    """The owned status view GET /v1/jobs/{job_id} answers."""
    row = await job_store.astatus_for_caller(payload.job_id, caller.caller_id)
    if row is None:
        raise_job_not_found(payload.job_id)
    return build_job_status(row)


async def control_job(
    payload: JobControlToolRequest, job_store, settings, shutdown_event, caller: CallerIdentity
) -> JobStatusResponse:
    """The pause/resume/cancel transition POST /v1/jobs/{job_id}/control runs."""
    return await control_job_core(payload.job_id, payload.action, job_store, settings, shutdown_event, caller)


async def delete_job_core(
    payload: JobIdRequest, job_store, settings, shutdown_event, caller: CallerIdentity
) -> JobDeletedResponse:
    """The REST delete door's cancel-then-tombstone, as a typed body."""
    if not await job_store.aexists_for_caller(payload.job_id, caller.caller_id):
        raise_job_not_found(payload.job_id)
    if job_runner.is_running(payload.job_id):
        job_runner.cancel_job_task(payload.job_id)
    if not await job_store.adelete_for_caller(payload.job_id, caller.caller_id):
        raise_job_not_found(payload.job_id)
    return JobDeletedResponse(deleted=True, job_id=payload.job_id)


__all__ = ["control_job", "create_job_core", "delete_job_core", "get_job_core", "list_jobs_core"]
