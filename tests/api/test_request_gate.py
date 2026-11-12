"""One owner for admit, reject, release: spelled per endpoint the three were
independently droppable, and a reject that skips ``end_request`` holds its
slot for the process's life -- nothing in the request path says why.
"""

import asyncio
import logging
import pathlib
import tempfile

import pytest
from fastapi import Response
from fastapi.responses import JSONResponse

import crawlutron.api.crawl_endpoint as crawl_endpoint
from crawlutron.api.caller_auth import Tracking
from crawlutron.api.crawl_endpoint import crawl
from crawlutron.api.identity_router import map as map_door
from crawlutron.config.settings import CrawlerSettings
from crawlutron.health.readiness import READINESS_STATE, ReadinessState
from crawlutron.models.caller_identity import CallerIdentity
from crawlutron.models.crawl_request import CrawlRequest
from crawlutron.models.map_request import MapRequest
from crawlutron.models.unified_crawl_response import UnifiedCrawlResponse
from crawlutron.runtime.metrics import get_metrics
from crawlutron.service import async_crawl_runner, main_helpers, map_endpoint
from crawlutron.service.caller_auth import CALLER_QUOTA_ADMISSION
from crawlutron.service.errors import DomainError
from crawlutron.service.main_helpers import request_gate
from crawlutron.storage.result_store import CrawlResultStore, CrawlSpillStore
from tests._job_harnesses import ScriptedExecute, make_job_store
from tests._result_builders import engine_result

logger = logging.getLogger(__name__)


def _active() -> int:
    return READINESS_STATE.snapshot().active_requests


def _denied_by_batch_cap() -> CrawlerSettings:
    from dataclasses import replace

    return replace(CrawlerSettings.from_mapping(), max_batch_size=1)


async def test_gate_holds_exactly_one_slot_for_the_body_and_releases_it():
    before = _active()
    held: list[int] = []
    with request_gate("test_request_rejected", logger=logger):
        held.append(_active())
    assert held == [before + 1]
    assert _active() == before


async def test_gate_releases_the_slot_when_the_body_is_rejected(caplog):
    """The rejection is a warning AND a 422 AND a release, or it is a bug.

    A reject that forgot the warning leaves an operator with a 422 and no
    server-side trace of why; one that forgot the release holds the slot.
    """
    before = _active()
    with caplog.at_level(logging.WARNING, logger=logger.name), pytest.raises(DomainError) as exc_info:
        with request_gate("test_request_rejected", logger=logger):
            raise ValueError("nothing crawlable here")
    assert exc_info.value.status == 422
    assert (exc_info.value.code, exc_info.value.message, exc_info.value.details) == (
        "validation_error",
        "Request validation failed",
        {"errors": [{"msg": "nothing crawlable here"}]},
    )
    assert "test_request_rejected" in caplog.text
    assert _active() == before


async def test_gate_releases_the_slot_when_the_body_fails_outright():
    """A non-validation failure is not converted, and is still released."""
    before = _active()
    with pytest.raises(RuntimeError), request_gate("test_request_rejected", logger=logger):
        raise RuntimeError("boom")
    assert _active() == before


def _real_stores(tmpdir: str):
    """The real store pair the production ``crawl`` signature promises."""
    return (
        CrawlResultStore(pathlib.Path(tmpdir) / "results.db"),
        CrawlSpillStore(pathlib.Path(tmpdir) / "spills.db"),
    )


async def test_rejected_crawl_hands_its_slot_back_and_still_counts_the_error(caplog):
    """The real /v1/crawl handler refusing a batch over the cap exercises the whole
    gate: the shared 422 envelope, the operator warning, and /v1/crawl's own error
    metric -- passed in as ``on_reject``, so a refactor dropping it stays visible.
    """
    metrics = get_metrics()
    before_active, before_errors = _active(), metrics.snapshot().crawl_errors_total

    # Real dependencies, not ``object()``. A store also asserts what the production ``crawl`` signature
    # promises it will be handed.
    with tempfile.TemporaryDirectory() as tmpdir:
        with caplog.at_level(logging.WARNING), pytest.raises(DomainError) as exc_info:
            result_store, spill_store = _real_stores(tmpdir)
            await crawl(
                CrawlRequest(urls=["http://example.com/", "http://example.com/2"]),
                _denied_by_batch_cap(),
                make_job_store(tmpdir),
                result_store,
                spill_store,
                asyncio.Event(),
            )

    assert exc_info.value.status == 422
    # This is the shape a client actually receives (the carrier IS the envelope triplet).
    assert exc_info.value.code == "validation_error"
    assert exc_info.value.details == {"errors": [{"msg": "batch_size_exceeded"}]}
    assert "crawl_request_rejected" in caplog.text
    assert _active() == before_active
    assert metrics.snapshot().crawl_errors_total == before_errors + 1


async def test_map_holds_a_readiness_slot_only_while_it_runs(monkeypatch):
    """The real /v1/map door samples the counter from inside the crawl it waits
    on, so the slot is proven taken for the work, not merely balanced -- a release
    before the body would pass a before/after check and hide a saturated service.
    """
    held: list[int] = []

    async def _record_slot(payload_dict, *, settings, shutdown_event, urls=None):
        held.append(_active())
        return {"results": []}

    monkeypatch.setattr(map_endpoint, "execute_crawl", _record_slot)

    before = _active()
    site_map = await map_door(
        MapRequest(url="http://example.com/"),
        CallerIdentity(),
        Tracking(),
        CALLER_QUOTA_ADMISSION,
        CrawlerSettings.from_mapping(),
        asyncio.Event(),
    )

    assert held == [before + 1]
    assert _active() == before
    assert site_map["links"] == []


async def test_request_gate_refuses_requests_over_the_cap(monkeypatch):
    """The configurable admission cap refuses with a 503 once active requests
    sit AT the cap; the refusal must not end_request the slot it never took."""
    state = ReadinessState(max_active_requests=2)
    assert state.begin_request() and state.begin_request()  # fill the cap
    monkeypatch.setattr(main_helpers, "readiness_state", state)

    with pytest.raises(DomainError) as exc_info:
        with request_gate("test_request_rejected", logger=logger):
            pass
    assert exc_info.value.status == 503
    assert exc_info.value.code == "capacity_exceeded"
    assert state.snapshot().active_requests == 2  # the refused request never held a slot


async def test_request_gate_admits_requests_under_the_cap(monkeypatch):
    """One active request under a cap of 2 is admitted and released normally."""
    state = ReadinessState(max_active_requests=2)
    assert state.begin_request()
    monkeypatch.setattr(main_helpers, "readiness_state", state)

    with request_gate("test_request_rejected", logger=logger):
        pass
    assert state.snapshot().active_requests == 1


async def test_request_gate_is_unbounded_without_a_cap(monkeypatch):
    """No cap configured (the default) never refuses: every request is admitted."""
    state = ReadinessState()
    for _ in range(10):
        assert state.begin_request() is True
    monkeypatch.setattr(main_helpers, "readiness_state", state)

    # No 503: unpinned states never refuse, and the gate's own slot is released.
    with request_gate("test_request_rejected", logger=logger):
        pass
    assert state.snapshot().active_requests == 10


def _crawl_call(tmpdir: str) -> CrawlRequest:
    """The one-crawl-per-test request body the slot tests share."""
    return CrawlRequest(urls=["http://example.test/"])


async def _run_crawl(
    payload: CrawlRequest, tmpdir: str, job_store
) -> JSONResponse | Response | UnifiedCrawlResponse:
    """The public crawl() door over one real per-test store pair."""
    result_store, spill_store = _real_stores(tmpdir)
    return await crawl(
        payload,
        CrawlerSettings.from_mapping(),
        job_store,
        result_store,
        spill_store,
        asyncio.Event(),
    )


async def test_crawl_holds_exactly_one_slot_for_the_whole_request(monkeypatch):
    """#576: a successful sync /v1/crawl samples the counter from inside the crawl it waits on, so the slot
    is proven taken once for the work, not twice -- a second nested gate would pass a before/after check
    only via the refusal it causes, so the count inside is what proves the double-acquire fixed.
    """
    held: list[int] = []

    async def _record_slot(payload_dict, *, settings, shutdown_event, url_pairs=None, spill_store=None):
        held.append(_active())
        return await ScriptedExecute(results=[])(
            payload_dict, settings=settings, shutdown_event=shutdown_event
        )

    monkeypatch.setattr(crawl_endpoint, "execute_crawl", _record_slot)

    before = _active()
    with tempfile.TemporaryDirectory() as tmpdir:
        await _run_crawl(_crawl_call(tmpdir), tmpdir, make_job_store(tmpdir))

    assert held == [before + 1]
    assert _active() == before


async def test_async_crawl_acceptance_holds_one_slot_and_releases_at_202(monkeypatch):
    """#576: the 202 door holds a slot while admitting/queueing and releases it at
    the 202 -- the durable row, not the readiness counter, owns the async work.
    """
    held: list[int] = []
    monkeypatch.setattr(async_crawl_runner, "run_async_crawl", lambda *a, **k: None)

    before = _active()
    with tempfile.TemporaryDirectory() as tmpdir:
        job_store = make_job_store(tmpdir)
        real_create = job_store.acreate_single_shot

        async def _sampling_create(*args, **kwargs):
            held.append(_active())  # sampled mid-queueing, under the outer gate
            return await real_create(*args, **kwargs)

        monkeypatch.setattr(job_store, "acreate_single_shot", _sampling_create)
        response = await _run_crawl(
            _crawl_call(tmpdir).model_copy(update={"async_mode": True}), tmpdir, job_store
        )

    # async_mode returns the durable-job 202 envelope, never a UnifiedCrawlResponse.
    assert isinstance(response, (JSONResponse, Response))
    assert response.status_code == 202
    assert held == [before + 1]
    assert _active() == before


async def test_crawl_succeeds_at_a_cap_of_one(monkeypatch):
    """#576: with CRAWLUTRON_MAX_ACTIVE_REQUESTS=1, a sync crawl still completes --
    a nested gate would see the outer slot taken and refuse 503 capacity_exceeded.
    """
    monkeypatch.setattr(main_helpers, "readiness_state", ReadinessState(max_active_requests=1))

    async def _execute(payload_dict, *, settings, shutdown_event, url_pairs=None, spill_store=None):
        return await ScriptedExecute(results=[engine_result("http://example.test/", "rendered")])(
            payload_dict, settings=settings, shutdown_event=shutdown_event
        )

    monkeypatch.setattr(crawl_endpoint, "execute_crawl", _execute)

    with tempfile.TemporaryDirectory() as tmpdir:
        response = await _run_crawl(_crawl_call(tmpdir), tmpdir, make_job_store(tmpdir))

    assert isinstance(response, UnifiedCrawlResponse)
