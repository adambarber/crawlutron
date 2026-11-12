"""Door-level delta tests: the sync ``/v1/crawl`` door classifies per result and rolls ``delta_summary`` up,
with caller-isolated baselines loaded from the store. No network, no async job runner: ``execute_crawl``
is swapped for a scripted stand-in so every engine result is exact.
"""

from __future__ import annotations

import asyncio
import pathlib

from crawlutron.config.settings import CrawlerSettings
from crawlutron.delta import CHANGED, NEW, UNCHANGED
from crawlutron.models.caller_identity import CallerIdentity
from crawlutron.models.crawl_request import CrawlRequest
from crawlutron.models.unified_crawl_response import UnifiedCrawlResponse
from crawlutron.storage.result_store import CrawlResultStore, CrawlSpillStore
from crawlutron_rest.api import crawl_endpoint as _crawl_endpoint_mod
from crawlutron_rest.api.crawl_endpoint import crawl
from tests._job_harnesses import ScriptedExecute, make_job_store, scripted_engine
from tests._result_builders import engine_result
from tests.delta._door_harness import assert_removed_summary, baseline_row, delta_stores, seed_baseline_row

URL = "http://example.com/"
ETAG_V1 = '"v1"'
ALIAS_URL = "HTTP://EXAMPLE.COM/page"
ALIAS_NORMALIZED = "http://example.com/page"
ALIAS_URL = "HTTP://EXAMPLE.COM/page"
ALIAS_NORMALIZED = "http://example.com/page"


def _stores(tmpdir: str):
    return delta_stores(tmpdir)


def _seed_baseline(result_store, request_id: str, caller_id: str, *, markdown: str) -> None:
    """Store a baseline response payload the delta loader will read back."""
    seed_baseline_row(
        result_store,
        request_id,
        owner=caller_id,
        results=[baseline_row((URL, URL), markdown=markdown)],
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
    html: str | None = None,
    headers: dict[str, str] | None = None,
    validators: dict[str, str] | None = None,
):
    return engine_result(
        URL,
        classification,
        http_status=http_status,
        markdown=markdown,
        html=html,
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


async def test_shape_change_between_baseline_and_recrawl_is_not_fingerprint_changed(monkeypatch, tmp_path):
    """A baseline stored at shape A (html stripped) compared against a re-crawl at
    shape B (include_html) must not classify changed via fingerprint_diff."""
    result_store, tmpdir = _seeded_single_row(tmp_path, URL, URL)
    payload = CrawlRequest(url=URL, baseline_request_id="baseline-1")
    response = await _run(
        monkeypatch,
        payload,
        tmpdir,
        results=[_result(markdown="one", html="one", validators={"etag": ETAG_V1})],
    )
    result = response.results[0]
    evidence = result.delta_evidence
    assert not (
        result.delta_classification == CHANGED
        and evidence is not None
        and evidence["why"] == "fingerprint_diff"
    )


def _baseline_run(monkeypatch, tmpdir: str, *, markdown: str, caller_id: str = "caller-a"):
    """A baseline row seeded for ``caller_id`` plus the request reusing it."""
    _seed_baseline(_stores(tmpdir)[0], "baseline-1", caller_id, markdown=markdown)
    return CrawlRequest(url=URL, baseline_request_id="baseline-1")


def _seeded_request(tmpdir: str) -> CrawlRequest:
    """Seed the shared baseline-1 row for caller-a and return its reuse request."""
    _seed_baseline(_stores(tmpdir)[0], "baseline-1", "caller-a", markdown="one")
    return CrawlRequest(url=URL, baseline_request_id="baseline-1")


def _seeded_single_row(tmp_path, url: str, resolved: str) -> tuple[CrawlResultStore, str]:
    """Seed one baseline-1 row over the given current+resolved URLs and return
    the store plus the directory: the single-row seed shape these tests reuse."""
    tmpdir = str(tmp_path)
    result_store, _ = _stores(tmpdir)
    seed_baseline_row(
        result_store,
        "baseline-1",
        results=[baseline_row((url, resolved), markdown="one")],
    )
    return result_store, tmpdir


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


async def test_expired_baseline_freshness_survives_on_wire(monkeypatch, tmp_path):
    """A missing baseline no longer masks changed_since: a Last-Modified after
    changed_since classifies changed, and the summary carries degraded_baseline."""
    payload = CrawlRequest(url=URL, baseline_request_id="does-not-exist", changed_since=1_735_689_600.0)
    newer = _result(headers={"last-modified": "Thu, 02 Jan 2025 00:00:00 GMT"}, markdown="body")
    response = await _run(monkeypatch, payload, str(tmp_path), results=[newer])
    assert response.results[0].delta_classification == CHANGED
    summary = response.delta_summary
    assert summary is not None and summary["changed"] == 1 and summary["degraded_baseline"] is True


async def test_degraded_marker_absent_on_healthy_baseline(monkeypatch, tmp_path):
    """A fresh baseline never marks the summary degraded: the field is additive."""
    tmpdir = str(tmp_path)
    payload = _seeded_request(tmpdir)
    response = await _run(monkeypatch, payload, tmpdir, results=[_result(markdown="one")])
    summary = response.delta_summary
    assert summary is not None and "degraded_baseline" not in summary


async def test_baseline_is_caller_scoped(monkeypatch, tmp_path):
    """The same baseline_request_id read by a different caller is foreign: the
    caller-scoped read misses, the request degrades, and the result is new."""
    tmpdir = str(tmp_path)
    payload = _seeded_request(tmpdir)
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
    seed_baseline_row(
        result_store,
        "baseline-1",
        results=[
            baseline_row((URL, URL), markdown="one"),
            baseline_row((removed_url, removed_url), markdown="two"),
        ],
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


async def test_baseline_alias_spelling_answered_is_not_removed(monkeypatch, tmp_path):
    """A baseline page whose ORIGINAL text differs from the resubmitted spelling
    is served when the run answers any of its identities: removed stays 0 and
    the page is never listed as not_in_admitted_set."""
    result_store, tmpdir = _seeded_single_row(tmp_path, ALIAS_URL, ALIAS_NORMALIZED)
    response = await _run(
        monkeypatch,
        CrawlRequest(url=ALIAS_NORMALIZED, baseline_request_id="baseline-1"),
        tmpdir,
        results=[_result(markdown="one")],
        result_store=result_store,
    )
    summary = response.delta_summary
    assert summary is not None
    assert summary["removed"] == 0
    skipped = summary["skipped"]
    assert isinstance(skipped, list)
    # Skipped shape: the one answered page appears at most once, and never
    # under a removal contradiction.
    removals = [row for row in skipped if row.get("why") == "not_in_admitted_set"]
    assert removals == []
    assert len([row for row in skipped if row.get("url") in (ALIAS_URL, ALIAS_NORMALIZED)]) <= 1
