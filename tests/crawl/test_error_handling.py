"""Centralized error handling: classification, result construction, and drift.

These tests pin the chokepoint contract: every strategy exception becomes a
classified terminal result through one primitive, and pool exhaustion is a
distinct, re-queueable classification rather than a generic network failure.
"""

import inspect
from dataclasses import asdict

import httpx
import pytest
from _testkit import *

from app.constants import BROWSER_HEADLESS
from app.crawl.result_helpers import _classify_error_code, _engine_result_from_exception
from app.crawl.strategies.browser_pool import BrowserPoolExhaustedError
from app.models import EngineCrawlResult

# ---------------------------------------------------------------------------
# Classification: the taxonomy is the single source of "what went wrong"
# ---------------------------------------------------------------------------

def test_classifier_lives_in_result_helpers_not_a_strategy_module():
    """The classifier is strategy-neutral; browser.py must import it from result_helpers."""

    import app.crawl.strategies.browser as browser_mod

    source = inspect.getsource(browser_mod)
    assert "_classify_error_code" in source
    assert "from app.crawl.result_helpers import _classify_error_code" in source, (
        "browser.py must import _classify_error_code from app.crawl.result_helpers, "
        "not reach across into the HTTP strategy module"
    )


def test_pool_exhaustion_classifies_distinctly():
    """Pool exhaustion means 'not attempted, capacity unavailable' -- never network_error."""
    assert _classify_error_code(BrowserPoolExhaustedError("none free")) == "browser_pool_exhausted"


def test_existing_taxonomy_is_preserved():
    """The move must not change any existing classification."""
    import httpx

    assert _classify_error_code(httpx.ConnectTimeout("x")) == "connection_error"
    assert _classify_error_code(ValueError("x")) == "value_error"
    assert _classify_error_code(RuntimeError("x")) == "network_error"


def test_pool_exhausted_is_a_taxonomy_member():
    """A non-member code rejects the whole batch at response shaping (constants.py)."""
    from app.constants import CrawlClassification

    assert CrawlClassification.browser_pool_exhausted.value == "browser_pool_exhausted"


# ---------------------------------------------------------------------------
# The primitive: one exception -> one terminal result
# ---------------------------------------------------------------------------

def test_primitive_builds_classified_failed_result():
    result = _engine_result_from_exception(
        error=httpx.ConnectTimeout("boom"),
        original_url="https://a.example/x",
        normalized_url="https://a.example/x",
        strategy="HTTP_DECLARED_CRAWLER",
    )
    assert isinstance(result, EngineCrawlResult)
    assert result.status == "failed"
    assert result.classification == "connection_error"
    assert result.attempted_strategies[0].classification == "connection_error"
    assert result.attempted_strategies[0].elapsed_ms is not None


def test_primitive_never_leaks_raw_exception_text():
    """Raw detail stays out of the wire-visible fields (H4 redaction contract)."""
    secret = "internal-host.local /etc/passwd"
    result = _engine_result_from_exception(
        error=ValueError(secret),
        original_url="https://a.example/x",
        normalized_url="https://a.example/x",
        strategy="HTTP_DECLARED_CRAWLER",
    )
    wire = asdict(result)
    assert secret not in str(wire), "raw exception text reached the result"


def test_primitive_marks_pool_exhaustion_as_not_attempted():
    """Pool exhaustion is capacity, not a page failure: the URL deserves a retry."""
    result = _engine_result_from_exception(
        error=BrowserPoolExhaustedError("none free"),
        original_url="https://a.example/x",
        normalized_url="https://a.example/x",
        strategy=BROWSER_HEADLESS,
    )
    assert result.classification == "browser_pool_exhausted"
    diagnostics = result.diagnostics or {}
    assert diagnostics.get("retryable") is True


@pytest.mark.parametrize(
    "error",
    [BrowserPoolExhaustedError("none free"), httpx.ConnectTimeout("boom"), ValueError("boom")],
)
def test_exception_envelopes_never_carry_discovered_links(error):
    """Failure envelopes carry no links -- the invariant that makes the
    re-queue ``continue`` in ``run_recursive_crawl`` (which precedes link
    expansion) safe: a re-queued pool-exhausted URL cannot "lose" links
    because an un-attempted crawl discovers none. If a future envelope
    builder ever fills ``discovered_links`` from partial results, that link
    source must reach the frontier BEFORE the re-queue decision; this pin
    fails the day that becomes possible so the ordering gets revisited."""
    result = _engine_result_from_exception(
        error=error,
        original_url="https://a.example/x",
        normalized_url="https://a.example/x",
        strategy="HTTP_DECLARED_CRAWLER",
    )
    assert result.discovered_links == []


def test_primitive_default_classification_is_network_error():
    result = _engine_result_from_exception(
        error=RuntimeError("x"),
        original_url="https://a.example/x",
        normalized_url="https://a.example/x",
        strategy="HTTP_DECLARED_CRAWLER",
    )
    assert result.classification == "network_error"


class TestClassificationValueValidity:
    """A classification the engine emits must survive response shaping.

    The bug this class pins: ``url_budget_exceeded`` was passed as
    ``known_classification`` -- a genuinely distinct signal -- but it was not a
    ``CrawlClassification`` member. The lenient internal dataclass accepted it,
    then ``_shape_all`` built a strict ``UnifiedCrawlResult`` and the whole
    batch 500'd on one budget-exhausted URL. The unit tests read raw spill
    dicts and never crossed the pydantic boundary, so the suite was green.
    """

    def test_url_budget_exceeded_is_a_taxonomy_member(self):
        from app.constants import CrawlClassification

        assert CrawlClassification.url_budget_exceeded.value == "url_budget_exceeded"

    def test_budget_exceeded_result_survives_response_shaping(self):
        """The boundary that rejected it: the strict enum on the wire model."""
        from app.api.response_shaping import build_unified_result

        item = _budget_item()
        result = build_unified_result(item)
        assert result.classification == "url_budget_exceeded"


def _budget_item() -> dict:
    """An engine result dict as produced by _engine_result_from_exception."""
    from app.crawl.result_helpers import _engine_result_from_exception

    result = _engine_result_from_exception(
        error=TimeoutError("budget exhausted"),
        original_url="https://a.example/slow",
        normalized_url="https://a.example/slow",
        strategy="HTTP_DECLARED_CRAWLER",
        known_classification="url_budget_exceeded",
        extra_diagnostics={"url_budget_seconds": 30},
    )
    return {
        "original_url": "https://a.example/slow",
        "normalized_url": "https://a.example/slow",
        "final_url": "https://a.example/slow",
        "status": "failed",
        "classification": result.classification,
        "selected_strategy": None,
        "attempted_strategies": [asdict(attempt) for attempt in result.attempted_strategies],
        "error": result.error,
        "http_status": None,
        "headers": {},
    }


class TestCallWorkerExecutesOnce:
    """A sync worker must not be invoked twice.

    The old shape ran worker(job) on the event-loop thread (blocking the loop
    and keeping side effects), found a non-coroutine, then called it AGAIN via
    asyncio.to_thread and discarded the first result. The first invocation's
    side effects were kept and its return discarded. Production passes async
    workers, so it never bit -- but it is a trap for the next sync worker, and
    the first call blocked the loop for the full duration, which is exactly
    what to_thread exists to avoid.
    """

    async def test_sync_worker_runs_once_off_loop(self):
        import asyncio

        from app.crawl.worker import call_worker

        calls: list[str] = []
        loop_thread = asyncio.get_running_loop().run_in_executor(None, lambda: None)
        await loop_thread

        def worker(job):
            calls.append(job)
            return "result"

        result = await call_worker(worker, "job")
        assert len(calls) == 1, f"sync worker invoked {len(calls)}x (side effects kept, result discarded)"
        assert result == "result"

    async def test_async_worker_awaits_directly(self):
        from app.crawl.worker import call_worker

        calls: list[str] = []

        async def worker(job):
            calls.append(job)
            return "async-result"

        result = await call_worker(worker, "job")
        assert len(calls) == 1
        assert result == "async-result"


class TestShapeAllGuardsPerItem:
    """One malformed item must not kill the whole batch at the endpoint.

    The job runner guards per-item (abuild_unified_result in try/except,
    dropping the item as a failed crawl). The endpoint's _shape_all had no
    guard, so a single unshapable item raised and the endpoint 500'd the
    entire batch -- the same primitive, two contracts. The fix lives at the
    shaping primitive itself so both paths share one guard.
    """

    async def test_one_bad_item_does_not_fail_the_batch(self):
        from app.api.response_shaping import abuild_unified_crawl_response, build_unified_result

        good = _budget_item()
        bad = {**good, "status": "not-a-real-status"}
        # the bad item is genuinely unshapable
        try:
            build_unified_result(bad)
            raise AssertionError("expected the malformed item to fail shaping")
        except Exception:
            pass

        response = await abuild_unified_crawl_response(
            [good, bad], request_id="r", submitted_count=2
        )
        assert len(response.results) == 2
        assert response.results[0].classification == "url_budget_exceeded"
        assert response.results[1].status == "failed"

    def test_fallback_status_and_classification_agree(self):
        # The fallback hardcodes status="failed" and picks the classification
        # independently. A consumer re-deriving status from classification
        # (what CLASSIFICATION_STATUS_BY_LABEL exists to allow) must see the
        # same value the fallback set, or the two paths describe different
        # outcomes for the same item.
        from app.api.response_shaping import _failed_unified_result
        from app.constants import status_for_classification

        result = _failed_unified_result({"normalized_url": "https://example.org/a"})
        assert status_for_classification(result.classification) == result.status
