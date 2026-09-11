"""Tests for ``app.api.webhooks`` module-level dependency wiring.

Verifies the P0 reviewer finding: the ``global`` statement in
``set_webhook_dependencies`` must cover all 5 module-level globals, and
``clear_webhook_dependencies`` must reset all of them. If either function
forgets a name, the corresponding module global stays at its previous
value, silently breaking webhook delivery at runtime.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from app.api import webhooks as wm


def test_set_webhook_dependencies_sets_all_globals():
    """Every module-level global is set; no local-variable shadowing."""
    # Reset to a known state first.
    wm.clear_webhook_dependencies()
    assert wm._webhook_semaphore is None
    assert wm._shutdown_event is None
    assert wm._crawl_request_fn is None
    assert wm._pool_getter is None
    assert wm._get_metrics_fn is None

    wm.set_webhook_dependencies(
        webhook_semaphore=asyncio.Semaphore(1),
        shutdown_event=asyncio.Event(),
        crawl_request_fn=lambda: None,
        pool_getter=lambda: None,
        get_metrics=lambda: None,
    )
    assert wm._webhook_semaphore is not None
    assert wm._shutdown_event is not None
    assert wm._crawl_request_fn is not None
    assert wm._pool_getter is not None
    assert wm._get_metrics_fn is not None


def test_clear_webhook_dependencies_resets_all_globals():
    """Teardown path resets all 5 globals; no leftover state across tests."""
    wm.set_webhook_dependencies(
        webhook_semaphore=asyncio.Semaphore(1),
        shutdown_event=asyncio.Event(),
        crawl_request_fn=MagicMock(),
        pool_getter=MagicMock(),
        get_metrics=MagicMock(),
    )
    wm.clear_webhook_dependencies()
    assert wm._webhook_semaphore is None
    assert wm._shutdown_event is None
    assert wm._crawl_request_fn is None
    assert wm._pool_getter is None
    assert wm._get_metrics_fn is None


def test_deliver_webhook_without_dependencies_raises():
    """Calling deliver_webhook before set_webhook_dependencies is a hard error.

    Guards against the silent-None-failure mode the P0 reviewer flagged:
    if ``global`` were incomplete, delivery would proceed with ``None``
    callbacks and crash somewhere downstream with an obscure error.
    """
    wm.clear_webhook_dependencies()
    with pytest.raises(RuntimeError, match="not initialized"):
        # asyncio.run to drive the coroutine
        asyncio.run(wm.deliver_webhook("http://example.com/cb", "rid", {}, MagicMock()))


def test_build_job_status_handles_single_shot_jobs():
    """SINGLE_SHOT jobs have empty frontier/visited (dataclass defaults).

    The reviewer was concerned that ``len(job.frontier)`` would raise
    AttributeError for SINGLE_SHOT jobs. Verified: ``frontier`` and
    ``visited`` are dataclass fields with default_factory, so they are
    always present even for jobs that never use them.
    """
    from app.api.main_helpers import _build_job_status
    from app.api_models import JobStatusResponse
    from app.crawl.job_store import CrawlJobState, JobKind

    job = CrawlJobState(
        job_id="single_test",
        kind=JobKind.SINGLE_SHOT,
        root_url="http://example.com/",
        policy={},
    )
    status = _build_job_status(job)
    assert isinstance(status, JobStatusResponse)
    assert status.frontier_size == 0
    assert status.visited_size == 0
    assert status.results_count == 0
    assert status.job_id == "single_test"
