"""Centralized error handling: classification, result construction, and drift.

Pins the chokepoint contract: every strategy exception becomes a classified
terminal result through one primitive, and pool exhaustion is re-queueable.
"""

from dataclasses import asdict, replace

import httpx
import pytest

from app.models.crawl_result import EngineCrawlResult
from app.models.host_policy import HostPolicy
from app.models.normalized_url import NormalizedUrl
from app.orchestration.classification import classify_error_code
from app.orchestration.results import engine_result_from_exception
from app.transport.browser_pool import BrowserPoolExhaustedError
from app.vocabulary.strategies import BROWSER_HEADED_WARMED, BROWSER_HEADLESS, HTTP_DECLARED_CRAWLER
from tests._browser_doubles import _fake_browser
from tests._fetcher_factories import failing_fetcher, raise_fetcher, run_chain
from tests._worker_doubles import (
    assert_coroutine_worker_is_awaited_directly,
    assert_sync_worker_returning_awaitable_is_named,
    assert_sync_worker_runs_once_in_a_thread,
)
from tests.crawl._chain_policies import chain_policy

# --------------------------------------------------------------------------- Classification: the taxonomy is
# the single source of "what went wrong"


def test_pool_exhaustion_classifies_distinctly():
    """Pool exhaustion means 'not attempted, capacity unavailable' -- never network_error."""
    assert classify_error_code(BrowserPoolExhaustedError("none free")) == "browser_pool_exhausted"


def test_existing_taxonomy_is_preserved():
    """The move must not change any existing classification."""
    assert classify_error_code(httpx.ConnectTimeout("x")) == "connection_error"
    assert classify_error_code(ValueError("x")) == "value_error"
    assert classify_error_code(RuntimeError("x")) == "network_error"


def test_pool_exhausted_is_a_taxonomy_member():
    """A non-member code rejects the whole batch at response shaping (constants.py)."""
    from app.vocabulary.taxonomy import CrawlClassification

    assert CrawlClassification.browser_pool_exhausted.value == "browser_pool_exhausted"


# --------------------------------------------------------------------------- The primitive: one exception ->
# one terminal result ---------------------------------------------------------------------------


def test_primitive_builds_classified_failed_result():
    result = engine_result_from_exception(
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
    """Raw detail stays out of the wire-visible fields."""
    secret = "internal-host.local /etc/passwd"
    result = engine_result_from_exception(
        error=ValueError(secret),
        original_url="https://a.example/x",
        normalized_url="https://a.example/x",
        strategy="HTTP_DECLARED_CRAWLER",
    )
    wire = asdict(result)
    assert secret not in str(wire), "raw exception text reached the result"


def test_primitive_marks_pool_exhaustion_as_not_attempted():
    """Pool exhaustion is capacity, not a page failure: the URL deserves a retry."""
    result = engine_result_from_exception(
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
    """Failure envelopes carry no links, making the re-queue ``continue`` safe:
    an un-attempted crawl discovers none. Newly discovered links must reach
    the frontier BEFORE the re-queue decision, or this pin fails.
    """
    result = engine_result_from_exception(
        error=error,
        original_url="https://a.example/x",
        normalized_url="https://a.example/x",
        strategy="HTTP_DECLARED_CRAWLER",
    )
    assert result.discovered_links == []


def test_primitive_default_classification_is_network_error():
    result = engine_result_from_exception(
        error=RuntimeError("x"),
        original_url="https://a.example/x",
        normalized_url="https://a.example/x",
        strategy="HTTP_DECLARED_CRAWLER",
    )
    assert result.classification == "network_error"


class TestClassificationValueValidity:
    """A classification the engine emits must survive response shaping.

    ``url_budget_exceeded`` passed as ``known_classification`` wasn't a member:
    the lenient dataclass took it, then the strict wire model 500'd the batch.
    """

    def test_url_budget_exceeded_is_a_taxonomy_member(self):
        from app.vocabulary.taxonomy import CrawlClassification

        assert CrawlClassification.url_budget_exceeded.value == "url_budget_exceeded"

    def test_budget_exceeded_result_survives_response_shaping(self):
        """The boundary that rejected it: the strict enum on the wire model."""
        from app.api.response_shaping import build_unified_result

        item = _budget_item()
        result = build_unified_result(item)
        assert result.classification == "url_budget_exceeded"


def _budget_item() -> EngineCrawlResult:
    result = engine_result_from_exception(
        error=TimeoutError("budget exhausted"),
        original_url="https://a.example/slow",
        normalized_url=NormalizedUrl("https://a.example/slow", policy=HostPolicy.unrestricted()),
        strategy="HTTP_DECLARED_CRAWLER",
        known_classification="url_budget_exceeded",
        extra_diagnostics={"url_budget_seconds": 30},
    )
    return result


class TestCallWorkerExecutesOnce:
    """A sync worker must not be invoked twice.

    The old shape ran it on the loop thread, then called it AGAIN via
    asyncio.to_thread and discarded the first result -- blocking the loop.
    """

    async def test_sync_worker_runs_once_off_loop(self):
        await assert_sync_worker_runs_once_in_a_thread()

    async def test_async_worker_awaits_directly(self):
        await assert_coroutine_worker_is_awaited_directly()

    async def test_a_sync_worker_returning_a_coroutine_is_named_at_the_seam(self):
        """The mistake is reported where it is made, not 100 lines downstream."""
        await assert_sync_worker_returning_awaitable_is_named()


class TestShapeAllGuardsPerItem:
    """One malformed item must not kill the whole batch at the endpoint.

    The job runner guards per-item, but the endpoint's ``_shape_all`` had no
    guard, so one item 500'd the batch; the fix lives at the primitive.
    """

    async def test_one_bad_item_does_not_fail_the_batch(self):
        from app.api.response_shaping import (
            EnvelopeOptions,
            abuild_unified_crawl_response,
            build_unified_result,
        )

        good = _budget_item()
        bad = replace(good, classification_evidence=[{"detail": object()}])
        # The bad item is a real result whose OPEN payload the wire model rejects, so shaping genuinely raises
        # on it.

        response = await abuild_unified_crawl_response(
            [good, bad],
            adapt=build_unified_result,
            options=EnvelopeOptions(request_id="r", submitted_count=2),
        )
        assert len(response.results) == 2
        assert response.results[0].classification == "url_budget_exceeded"
        assert response.results[1].status == "failed"

    def test_fallback_status_and_classification_agree(self):
        # The fallback hardcodes status="failed" and picks the classification independently.
        from app.api.response_shaping import failed_unified_result
        from app.vocabulary.taxonomy import status_for_classification

        result = failed_unified_result({"normalized_url": "https://example.org/a"})
        assert status_for_classification(result.classification) == result.status


# --------------------------------------------------------------------------- The chain guard: a strategy that
# RAISES is a failed STEP, not a dead URL
_rendered = _fake_browser(
    html="<html><body><main>rendered page body</main></body></html>",
    text="rendered page body",
)

_unreachable_fetcher = failing_fetcher(AssertionError("an HTTP strategy ran but should not have"))


class TestStrategyChainGuardsRaisingStrategies:
    """A raise at the strategy ADAPTER boundary becomes a failed step, not a dead URL.

    The HTTP and browser workers each catch inside; a raise between them
    escaped the chain and skipped every remaining ladder step.
    """

    async def test_a_raising_step_is_enveloped_and_the_ladder_continues(self):

        attempts: list[str] = []

        class _FlakyWorker:
            async def __call__(self, job):
                attempts.append(job.url.raw)
                if len(attempts) == 1:
                    raise RuntimeError("camoufox binding vanished mid-render")
                return _rendered(job)

        result = await run_chain(
            fetcher=_unreachable_fetcher,
            browser_worker=_FlakyWorker(),
            policy=chain_policy(BROWSER_HEADLESS, BROWSER_HEADED_WARMED),
        )

        assert len(attempts) == 2, "the ladder must continue past a raising step"
        assert result.status == "success"
        assert result.selected_strategy == BROWSER_HEADED_WARMED
        # The raising step is recorded as an ATTEMPT, with the classification the shared primitive derived --
        # that is what a consumer reads the ladder from.
        assert [(a.strategy, a.classification) for a in result.attempted_strategies] == [
            (BROWSER_HEADLESS, "network_error"),
            (BROWSER_HEADED_WARMED, "rendered"),
        ]

    async def test_a_raising_final_step_is_a_classified_terminal_result(self):

        async def raising_worker(job):
            raise RuntimeError("internal-host.local /etc/passwd")

        result = await run_chain(
            fetcher=_unreachable_fetcher,
            browser_worker=raising_worker,
            policy=chain_policy(BROWSER_HEADLESS),
        )

        assert result.status == "failed"
        assert result.classification == "network_error"
        # The stable code on the wire, and the primitive's redaction contract holds at this call site too.
        assert result.error == "network_error"
        assert "internal-host.local" not in str(asdict(result))

    async def test_the_guard_does_not_re_wrap_the_http_strategy_sink(self):
        """The HTTP strategy already envelopes its own raise; the chain guard
        must leave that result exactly as the sink built it."""
        result = await run_chain(
            fetcher=raise_fetcher,
            policy=chain_policy(HTTP_DECLARED_CRAWLER),
        )

        assert result.status == "failed"
        assert result.attempted_strategies[0].strategy == HTTP_DECLARED_CRAWLER
        assert result.classification == "connection_error"
        assert result.error == "connection_error"
