"""``GET /v1/crawl/{request_id}/trace``: the diagnostic spine of a finished crawl.

The trace is a declared projection of the record the crawl already stored --
same request, same attempt list, same classification evidence -- with the page
bodies left out. So a trace cannot disagree with the response it describes:
there is one record, read two ways. These tests drive a real crawl and read the
trace back through the API, because the interesting failure is a projection
that drifts from the stored shape, and only a stored shape can show that.
"""

import pytest
from fastapi import HTTPException

from app.api.crawl_endpoint import get_crawl_trace
from app.crawl.job_store import JobStatus
from app.crawl.result_store import CrawlResultStore
from app.vocabulary.strategies import HTTP_DECLARED_CRAWLER
from tests._assertions import completed_crawl
from tests._fetcher_factories import make_crawl_payload
from tests._policy_factories import canonical


@pytest.fixture
def result_store(tmp_path) -> CrawlResultStore:
    return CrawlResultStore(tmp_path / "results.db")


async def test_a_trace_reports_every_attempted_strategy(async_client, base_url):
    crawled = await completed_crawl(async_client, make_crawl_payload(base_url))

    traced = await async_client.get(f"/v1/crawl/{crawled['request_id']}/trace")

    assert traced.status_code == 200, traced.text
    trace = traced.json()
    assert trace["request_id"] == crawled["request_id"]
    page = trace["results"][0]
    assert page["normalized_url"] == canonical(base_url).raw
    assert page["status"] == "success"
    # The spans are the crawl's own attempt list, so the strategy that ran and
    # the verdict it reached are the ones the response reported.
    assert [span["strategy"] for span in page["attempted_strategies"]] == [HTTP_DECLARED_CRAWLER]
    assert page["attempted_strategies"][0]["http_status"] == 200
    assert page["attempted_strategies"][0]["elapsed_ms"] >= 0
    assert isinstance(page["classification_evidence"], list)


async def test_a_trace_carries_no_page_body(async_client, base_url):
    """The trace is the diagnostic spine: a caller who wants the page still asks
    ``/v1/crawl/{request_id}``, and the two do not duplicate megabytes."""
    crawled = await completed_crawl(async_client, make_crawl_payload(base_url))

    page = (await async_client.get(f"/v1/crawl/{crawled['request_id']}/trace")).json()["results"][0]

    for absent in ("html", "text", "markdown", "metadata", "discovered_links"):
        assert absent not in page, f"the trace carried {absent}"


async def test_an_unknown_request_id_has_no_trace(result_store, job_store):
    with pytest.raises(HTTPException) as raised:
        await get_crawl_trace("crawl_never_happened", job_store, result_store)

    assert raised.value.status_code == 404, raised.value.detail


async def test_a_running_job_is_still_pending_for_its_trace(job_store, result_store):
    """The trace is a view of a finished crawl, so it refuses the same way the
    status door does: a crawl mid-flight has no trace to show, and answering
    anyway would report work in progress as though it were done."""
    await job_store.acreate_single_shot("crawl_running", "http://example.com/", {})
    running = await job_store.aload("crawl_running")
    running.results = [
        {
            "request_id": "crawl_running",
            "results": [],
            "submitted_count": 0,
            "policy_schema_version": "",
            "success": True,
        }
    ]
    await job_store.asave(running)

    with pytest.raises(HTTPException) as raised:
        await get_crawl_trace("crawl_running", job_store, result_store)

    assert raised.value.status_code == 202, raised.value.detail


async def test_a_finished_single_shot_job_answers_its_trace_from_the_job_row(job_store, result_store):
    """An async crawl's result lives on the job row as well as the cache, and the
    cache expires; a trace that only read the cache would 404 a crawl whose
    record is still durably there."""
    await job_store.acreate_single_shot("crawl_done", "http://example.com/", {})
    done = await job_store.aload("crawl_done")
    done.results = [
        {
            "request_id": "crawl_done",
            "results": [
                {
                    "normalized_url": "http://example.com/",
                    "status": "success",
                    "classification": "rendered",
                    "selected_strategy": HTTP_DECLARED_CRAWLER,
                    "attempted_strategies": [
                        {
                            "strategy": HTTP_DECLARED_CRAWLER,
                            "status": "success",
                            "classification": "rendered",
                            "http_status": 200,
                            "content_type": "text/html",
                            "elapsed_ms": 7,
                            "error": None,
                        }
                    ],
                    "classification_evidence": [],
                }
            ],
            "submitted_count": 1,
            "policy_schema_version": "",
            "success": True,
        }
    ]
    await job_store.asave(done)
    await job_store.aset_status("crawl_done", JobStatus.COMPLETED, expected=JobStatus.PENDING)

    trace = await get_crawl_trace("crawl_done", job_store, result_store)

    assert trace.request_id == "crawl_done"
    assert trace.results[0].attempted_strategies[0].elapsed_ms == 7
