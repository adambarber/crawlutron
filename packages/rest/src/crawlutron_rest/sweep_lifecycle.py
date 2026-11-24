"""Process-scope ownership of the four lifespan maintenance sweeps."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from crawlutron_admit import Count, Duration
from crawlutron_admit.task_scope import Grace, NamedRegistry, ScopeRefused, TaskScope

if TYPE_CHECKING:
    from fastapi import FastAPI

    from crawlutron.config.settings import CrawlerSettings

logger = logging.getLogger(__name__)

_SWEEP_GRACE_SECONDS = 5.0


async def start_sweeps(app: FastAPI, settings: CrawlerSettings, shutdown_event: asyncio.Event) -> TaskScope:
    """Spawn the four sweeps under one process scope; keep task attrs for readers."""
    from crawlutron.service.cache_sweep import sweep_cache_forever
    from crawlutron.service.retention_sweep import sweep_retention_forever
    from crawlutron.service.spill_sweep import sweep_spills_forever
    from crawlutron.service.watch_sweep import sweep_watches_forever
    from safe_url.host_policy import HostPolicy

    scope = TaskScope.process(
        registry=NamedRegistry("process:sweeps", Count(4), Duration(0.25)),
        grace=Grace(_SWEEP_GRACE_SECONDS),
    )
    app.state.sweep_scope = scope
    app.state.cache_maintenance_task = (await scope.spawn(lambda: sweep_cache_forever(shutdown_event))).task
    ttl = app.state.result_store.ttl_seconds
    app.state.spill_maintenance_task = (
        await scope.spawn(
            lambda: sweep_spills_forever(
                app.state.spill_store, shutdown_event=shutdown_event, max_age_seconds=ttl
            )
        )
    ).task
    app.state.retention_sweep_task = (
        await scope.spawn(
            lambda: sweep_retention_forever(
                app.state.job_store,
                app.state.result_store,
                shutdown_event,
                grace_seconds=settings.retention_sweep_grace_seconds,
            )
        )
    ).task
    app.state.watch_sweep_task = (
        await scope.spawn(
            lambda: sweep_watches_forever(
                app.state.watch_store,
                host_policy=HostPolicy.from_settings(settings),
                settings=settings,
                shutdown_event=shutdown_event,
                key_store=app.state.caller_key_store,
                concurrency_tracker=app.state.concurrency_tracker,
                job_store=app.state.job_store,
            )
        )
    ).task
    return scope


async def stop_sweeps(scope: TaskScope) -> None:
    """Cancel and await every sweep through the door before stores close."""
    try:
        await scope.__aexit__(None, None, None)
    except ScopeRefused as refused:
        logger.warning("sweep_stop_refused", extra={"reason": refused.reason.value})
