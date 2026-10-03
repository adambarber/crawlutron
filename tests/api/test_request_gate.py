"""The endpoint readiness gate: one owner for admit, reject, release.

``/v1/crawl``, ``/v1/map``, and ``/v1/render`` each hand their request to
:func:`app.api.main_helpers.request_gate`, which admits a readiness slot,
converts a ``ValueError`` rejection into the shared 4xx envelope with an
operator warning, and releases the slot. Spelled per endpoint, the three
obligations were independently droppable and the expensive one to lose is the
release: a rejected request that skips ``end_request`` holds its slot for the
life of the process, so ``/ready`` never recovers from it and nothing in the
request path says why.

These tests drive the real gate, the real endpoints, and the real readiness
singleton -- the counter is process-wide, so every assertion here is a delta
rather than an absolute.
"""

import asyncio
import logging
import pathlib
import tempfile

import pytest
from fastapi import HTTPException

from app.api import map_endpoint
from app.api.crawl_endpoint import crawl
from app.api.exceptions import error_envelope_from_detail
from app.api.main_helpers import request_gate
from app.api.map_endpoint import map_site
from app.crawl.result_store import CrawlResultStore
from app.health.readiness import READINESS_STATE
from app.metrics import get_metrics
from app.models.crawl_request import CrawlRequest
from app.models.map_request import MapRequest
from app.settings import CrawlerSettings
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
    """The real /v1/crawl handler refusing a batch over the cap.

    Exercises the whole gate: the shared 422 envelope, the operator warning, the
    released slot, and /v1/crawl's own error metric -- which the handler used to
    bump from its own reject tail and now passes in as the gate's ``on_reject``
    hook, so a refactor that dropped it would be invisible here otherwise.
    """
    metrics = get_metrics()
    before_active, before_errors = _active(), metrics.crawl_errors_total

    # Real dependencies, not ``object()``. The gate refuses before it reaches
    # any of them, and that is the claim under test -- so passing a stand-in
    # made true an accident rather than a fact: the call had to be type-correct
    # for "never touched" to mean anything. A store also asserts what the
    # production ``crawl`` signature promises it will be handed.
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
    # Through the accessor the service itself uses, rather than indexing
    # ``detail``: ``HTTPException.detail`` is declared ``str | None`` and carries
    # an envelope dict here, so a direct index was reaching past what the type
    # says is there. This is the shape a client actually receives.
    envelope = error_envelope_from_detail(exc_info.value.detail, exc_info.value.status_code)
    assert envelope["code"] == "validation_error"
    assert envelope["details"] == {"errors": [{"msg": "batch_size_exceeded"}]}
    assert "crawl_request_rejected" in caplog.text
    assert _active() == before_active
    assert metrics.crawl_errors_total == before_errors + 1


async def test_map_holds_a_readiness_slot_only_while_it_runs(monkeypatch):
    """The real /v1/map handler on the success path, at the engine seam.

    The counter is sampled from inside the crawl the endpoint is waiting on, so
    this proves the slot is taken for the work and not merely balanced at the
    end -- a release that fired before the body would pass a before/after check
    alone and report a saturated service as idle.
    """
    held: list[int] = []

    async def _record_slot(payload_dict, *, settings, shutdown_event, urls=None):
        held.append(_active())
        return {"results": []}

    monkeypatch.setattr(map_endpoint, "execute_crawl", _record_slot)

    before = _active()
    site_map = await map_site(MapRequest(url="http://example.com/"), settings=CrawlerSettings.from_mapping())

    assert held == [before + 1]
    assert _active() == before
    assert site_map["links"] == []
