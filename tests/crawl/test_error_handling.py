"""Centralized error handling: classification, result construction, and drift.

These tests pin the chokepoint contract: every strategy exception becomes a
classified terminal result through one primitive, and pool exhaustion is a
distinct, re-queueable classification rather than a generic network failure.
"""

import inspect
from dataclasses import asdict

import httpx
from _testkit import *

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
        strategy="BROWSER_DECLARED_CRAWLER",
    )
    assert result.classification == "browser_pool_exhausted"
    diagnostics = result.diagnostics or {}
    assert diagnostics.get("retryable") is True


def test_primitive_default_classification_is_network_error():
    result = _engine_result_from_exception(
        error=RuntimeError("x"),
        original_url="https://a.example/x",
        normalized_url="https://a.example/x",
        strategy="HTTP_DECLARED_CRAWLER",
    )
    assert result.classification == "network_error"
