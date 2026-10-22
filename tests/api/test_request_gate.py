"""One owner for admit, reject, release: spelled per endpoint the three were
independently droppable, and a reject that skips ``end_request`` holds its
slot for the process's life -- nothing in the request path says why.
"""

import asyncio
import logging
import pathlib
import tempfile

import pytest
from fastapi import HTTPException

from app.api import main_helpers, map_endpoint
from app.api.caller_auth import Tracking
from app.api.crawl_endpoint import crawl
from app.api.exceptions import error_envelope_from_detail
from app.api.identity_router import map as map_door
from app.api.main_helpers import request_gate
from app.config.settings import CrawlerSettings
from app.health.readiness import READINESS_STATE, ReadinessState
from app.models.caller_identity import CallerIdentity
from app.models.crawl_request import CrawlRequest
from app.models.map_request import MapRequest
from app.runtime.metrics import get_metrics
from app.storage.result_store import CrawlResultStore
from tests._job_harnesses import make_job_store

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
    with caplog.at_level(logging.WARNING, logger=logger.name), pytest.raises(HTTPException) as exc_info:
        with request_gate("test_request_rejected", logger=logger):
            raise ValueError("nothing crawlable here")
    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == {
        "code": "validation_error",
        "message": "Request validation failed",
        "details": {"errors": [{"msg": "nothing crawlable here"}]},
    }
    assert "test_request_rejected" in caplog.text
    assert _active() == before


async def test_gate_releases_the_slot_when_the_body_fails_outright():
    """A non-validation failure is not converted, and is still released."""
    before = _active()
    with pytest.raises(RuntimeError), request_gate("test_request_rejected", logger=logger):
        raise RuntimeError("boom")
    assert _active() == before


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
        with caplog.at_level(logging.WARNING), pytest.raises(HTTPException) as exc_info:
            await crawl(
                CrawlRequest(urls=["http://example.com/", "http://example.com/2"]),
                _denied_by_batch_cap(),
                make_job_store(tmpdir),
                CrawlResultStore(pathlib.Path(tmpdir) / "results.db"),
                asyncio.Event(),
            )

    assert exc_info.value.status_code == 422
    # This is the shape a client actually receives.
    envelope = error_envelope_from_detail(exc_info.value.detail, exc_info.value.status_code)
    assert envelope["code"] == "validation_error"
    assert envelope["details"] == {"errors": [{"msg": "batch_size_exceeded"}]}
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

    with pytest.raises(HTTPException) as exc_info:
        with request_gate("test_request_rejected", logger=logger):
            pass
    assert exc_info.value.status_code == 503
    envelope = error_envelope_from_detail(exc_info.value.detail, exc_info.value.status_code)
    assert envelope["code"] == "capacity_exceeded"
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
