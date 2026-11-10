"""Door-level delta tests: the sync ``/v1/crawl`` door classifies per result and
rolls ``delta_summary`` up, with caller-isolated baselines loaded from the store.

No network, no async job runner: ``execute_crawl`` is swapped for a scripted
stand-in so every engine result is exact.
"""

from __future__ import annotations

import asyncio
import pathlib

from crawlutron.api import crawl_endpoint as _crawl_endpoint_mod
from crawlutron.api.crawl_endpoint import crawl
from crawlutron.config.settings import CrawlerSettings
from crawlutron.delta import CHANGED, NEW, UNCHANGED
from crawlutron.models.caller_identity import CallerIdentity
from crawlutron.models.crawl_request import CrawlRequest
from crawlutron.models.unified_crawl_response import UnifiedCrawlResponse
from crawlutron.storage.result_store import CrawlResultStore, CrawlSpillStore
from tests._job_harnesses import ScriptedExecute, make_job_store, scripted_engine
from tests._result_builders import engine_result
from tests.delta._door_harness import assert_removed_summary, delta_stores

URL = "http://example.com/"
ETAG_V1 = '"v1"'


def _stores(tmpdir: str):
    return delta_stores(tmpdir)


def _seed_baseline(result_store, request_id: str, caller_id: str, *, markdown: str) -> None:
    """Store a baseline response payload the delta loader will read back."""
    result_store.put(
        request_id,
        {
            "request_id": request_id,
            "results": [
                {
                    "original_url": URL,
                    "normalized_url": URL,
                    "validators": {"etag": ETAG_V1},
                    "markdown": markdown,
                    "html": markdown,
                    "text": markdown,
                }
            ],
        },
        owner=caller_id,
    )


async def _run(
    monkeypatch,
    payload: CrawlRequest,
    tmpdir: str,
    *,
    results=None,
    caller_id: str = "caller-a",
    result_store: CrawlResultStore | None = None,
) -> UnifiedCrawlResponse:
    """Run the sync crawl() door over real stores with a scripted engine."""
    if result_store is None:
        result_store, spill_store = _stores(tmpdir)
    else:
        spill_store = CrawlSpillStore(pathlib.Path(tmpdir) / "spills.db")
    scripted = ScriptedExecute(results=results or [])
    scripted_engine(monkeypatch, _crawl_endpoint_mod, scripted)
    response = await crawl(
        payload,
        CrawlerSettings.from_mapping(),
        make_job_store(tmpdir),
        result_store,
        spill_store,
        asyncio.Event(),
        caller=CallerIdentity(caller_id=caller_id),
    )
    assert isinstance(response, UnifiedCrawlResponse)
    return response


def _result(
    classification: str = "rendered",
    *,
    http_status: int = 200,
    markdown: str | None = None,
    headers: dict[str, str] | None = None,
    validators: dict[str, str] | None = None,
):
    return engine_result(
        URL,
        classification,
        http_status=http_status,
        markdown=markdown,
        headers=headers or {},
        validators=validators or {},
    )


async def test_no_behavior_change_when_knobs_unset(monkeypatch, tmp_path):
    response = await _run(
        monkeypatch, CrawlRequest(url=URL), str(tmp_path), results=[_result(markdown="body")]
    )
    result = response.results[0]
    assert result.delta_classification is None
    assert result.delta_evidence is None
    assert response.delta_summary is None


async def test_304_against_baseline_is_unchanged(monkeypatch, tmp_path):
    tmpdir = str(tmp_path)
    payload = _baseline_run(monkeypatch, tmpdir, markdown="one")
    result = _result(classification="not_modified", http_status=304, markdown="one")
    response = await _run(monkeypatch, payload, tmpdir, results=[result])
    assert response.results[0].delta_classification == UNCHANGED
    evidence = response.results[0].delta_evidence
    assert evidence is not None
    assert evidence["why"] == "304_not_modified"
    assert evidence["matched_validators"] is True
    summary = response.delta_summary
    assert summary is not None and summary["unchanged"] == 1 and summary["new"] == 0


async def test_changed_fingerprint_is_changed(monkeypatch, tmp_path):
    tmpdir = str(tmp_path)
    payload = _baseline_run(monkeypatch, tmpdir, markdown="one")
    response = await _run(monkeypatch, payload, tmpdir, results=[_result(markdown="two")])
    assert response.results[0].delta_classification == CHANGED
    evidence = response.results[0].delta_evidence
    assert evidence is not None and evidence["why"] == "fingerprint_diff"
    summary = response.delta_summary
    assert summary is not None and summary["changed"] == 1


def _baseline_run(monkeypatch, tmpdir: str, *, markdown: str, caller_id: str = "caller-a"):
    """A baseline row seeded for ``caller_id`` plus the request reusing it."""
    _seed_baseline(_stores(tmpdir)[0], "baseline-1", caller_id, markdown=markdown)
    return CrawlRequest(url=URL, baseline_request_id="baseline-1")


async def test_older_last_modified_is_unchanged(monkeypatch, tmp_path):
    payload = CrawlRequest(url=URL, changed_since=1_735_689_600.0)  # 2025-01-01T00:00:00Z
    result = _result(headers={"last-modified": "Wed, 01 Jan 2025 00:00:00 GMT"}, markdown="body")
    response = await _run(monkeypatch, payload, str(tmp_path), results=[result])
    assert response.results[0].delta_classification == UNCHANGED
    summary = response.delta_summary
    assert summary is not None and summary["unchanged"] == 1 and summary["considered"] == 1


async def test_missing_baseline_degrades_to_baseline_expired_new(monkeypatch, tmp_path):
    payload = CrawlRequest(url=URL, baseline_request_id="does-not-exist")
    response = await _run(monkeypatch, payload, str(tmp_path), results=[_result(markdown="body")])
    assert response.results[0].delta_classification == NEW
    evidence = response.results[0].delta_evidence
    assert evidence is not None and evidence["why"] == "baseline_expired"
    summary = response.delta_summary
    assert summary is not None and summary["new"] == 1


async def test_baseline_is_caller_scoped(monkeypatch, tmp_path):
    """The same baseline_request_id read by a different caller is foreign: the
    caller-scoped read misses, the request degrades, and the result is new."""
    tmpdir = str(tmp_path)
    _seed_baseline(_stores(tmpdir)[0], "baseline-1", "caller-a", markdown="one")
    payload = CrawlRequest(url=URL, baseline_request_id="baseline-1")
    own = await _scoped_run(monkeypatch, payload, tmpdir, "caller-a")
    foreign = await _scoped_run(monkeypatch, payload, tmpdir, "caller-b")
    assert own.results[0].delta_classification == UNCHANGED
    assert foreign.results[0].delta_classification == NEW
    evidence = foreign.results[0].delta_evidence
    assert evidence is not None and evidence["why"] == "baseline_expired"


async def _scoped_run(
    monkeypatch, payload: CrawlRequest, tmpdir: str, caller_id: str
) -> UnifiedCrawlResponse:
    """The baseline run under ``caller_id``'s isolation scope."""
    return await _run(
        monkeypatch,
        payload,
        tmpdir,
        results=[_result(classification="not_modified", http_status=304, markdown="one")],
        caller_id=caller_id,
    )


async def test_baseline_url_absent_from_run_is_removed(monkeypatch, tmp_path):
    """A baseline URL this run never answers is the removed verdict: the summary
    counts it and skipped carries why=not_in_admitted_set for it."""
    tmpdir = str(tmp_path)
    removed_url = "http://example.com/gone/"
    result_store, _ = _stores(tmpdir)
    result_store.put(
        "baseline-1",
        {
            "request_id": "baseline-1",
            "results": [
                {
                    "original_url": URL,
                    "normalized_url": URL,
                    "validators": {"etag": ETAG_V1},
                    "markdown": "one",
                },
                {
                    "original_url": removed_url,
                    "normalized_url": removed_url,
                    "validators": {"etag": ETAG_V1},
                    "markdown": "two",
                },
            ],
        },
        owner="caller-a",
    )
    response = await _run(
        monkeypatch,
        CrawlRequest(url=URL, baseline_request_id="baseline-1"),
        tmpdir,
        results=[_result(markdown="one")],
        result_store=result_store,
    )
    assert response.delta_summary is not None
    assert_removed_summary(response.delta_summary, removed_url)
