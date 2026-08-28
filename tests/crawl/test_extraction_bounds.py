import os
import time

import pytest

import app.crawl.extraction as ext_mod
from app.crawl.extraction import _extract_document, _EXTRACTION_DOC_TIMEOUT, _MAX_EXTRACTED_MARKDOWN


@pytest.fixture(autouse=True)
def _reset_doc_extraction_pool():
    """No persistent process pool to reset.

    Each extraction call now spawns its own short-lived process (reaped on
    completion or timeout), so there is nothing to tear down between tests. The
    injected env flags (TEST_MARKITDOWN_HANG / TEST_MARKITDOWN_OUTPUT) are
    reverted by the ``monkeypatch`` fixture at test teardown.
    """
    yield


async def test_extract_document_timeout_is_enforced(monkeypatch, real_sleeps):
    """A document extraction exceeding the wall-clock timeout returns None (F5).

    Uses ``real_sleeps`` because the slow extraction is simulated with a real
    ``time.sleep`` which the autouse ``fast_sleeps`` no-ops.
    """
    # Use env var to signal test hang to the worker subprocess (inherited).
    monkeypatch.setenv("TEST_MARKITDOWN_HANG", "1")
    # Set short timeout - patch the module variable directly.
    monkeypatch.setattr(ext_mod, "_EXTRACTION_DOC_TIMEOUT", 0.01)

    result = _extract_document(b"%PDF-1.4 fake", "http://example.com/x.pdf")
    assert result.text is None
    assert result.markdown is None

    # Clean up
    monkeypatch.delenv("TEST_MARKITDOWN_HANG", raising=False)


async def test_extract_document_output_capped_at_5mb(monkeypatch):
    """Extracted markdown is truncated to _MAX_EXTRACTED_MARKDOWN (F5)."""
    # Use a smaller cap for faster test (10KB instead of 5MB)
    test_cap = 10000
    huge = "x" * (test_cap + 1000)

    # Use env var to inject test data into the worker subprocess.
    monkeypatch.setenv("TEST_MARKITDOWN_OUTPUT", huge)
    # Set the cap directly
    monkeypatch.setattr(ext_mod, "_MAX_EXTRACTED_MARKDOWN", test_cap)

    result = _extract_document(b"%PDF-1.4 fake", "http://example.com/x.pdf")
    assert result.markdown is not None
    assert len(result.markdown) == test_cap

    monkeypatch.delenv("TEST_MARKITDOWN_OUTPUT", raising=False)


async def test_extract_document_normal_works(monkeypatch):
    """Normal extraction returns the markdown content."""
    # Use env var to inject test data into the worker subprocess.
    monkeypatch.setenv("TEST_MARKITDOWN_OUTPUT", "## Hello Markdown")

    result = _extract_document(b"%PDF-1.4 fake", "http://example.com/x.pdf")
    assert result.markdown == "## Hello Markdown"
    assert result.text == "## Hello Markdown"

    monkeypatch.delenv("TEST_MARKITDOWN_OUTPUT", raising=False)


# ============================================================
# Adversarial perf #3.2: document extraction runs in a process that is
# forcefully killed on timeout (no lingering zombie / stalled pool).
# ============================================================

async def test_extract_document_process_pool_isolation(monkeypatch, real_sleeps):
    """Document extraction runs in a separate process so a hang can be killed
    and does not degrade the shared thread pool.
    """
    # Use a very short timeout - patch the module variable directly.
    monkeypatch.setattr(ext_mod, "_EXTRACTION_DOC_TIMEOUT", 0.01)
    from app.crawl.extraction import _EXTRACTION_DOC_TIMEOUT

    assert _EXTRACTION_DOC_TIMEOUT == 0.01

    # The subprocess will check this env var and hang if set.
    monkeypatch.setenv("TEST_MARKITDOWN_HANG", "1")

    result = _extract_document(b"%PDF-1.4 fake", "http://example.com/x.pdf")
    # Should return None due to timeout (process killed).
    assert result.text is None
    assert result.markdown is None

    # Clean up env var for other tests
    monkeypatch.delenv("TEST_MARKITDOWN_HANG", raising=False)


async def test_extract_document_process_pool_normal_works(monkeypatch):
    """Normal document extraction still works with process isolation."""
    # Use normal timeout - patch the module variable directly.
    monkeypatch.setattr(ext_mod, "_EXTRACTION_DOC_TIMEOUT", 10.0)
    from app.crawl.extraction import _extract_document

    # Don't set TEST_MARKITDOWN_HANG - normal operation.
    result = _extract_document(b"%PDF-1.4 fake", "http://example.com/x.pdf")
    # With fake bytes, markitdown returns something (or None for unparseable);
    # the key is it doesn't crash.
    assert isinstance(result, ext_mod.ExtractedContent)

async def test_extract_document_recovers_after_timeout(monkeypatch, real_sleeps):
    """After a timed-out (killed) extraction, the next call still works.

    Adversarial perf #3.2: a killed worker must not leave a zombie that stalls
    the (semaphore-bounded) extraction concurrency.
    """
    # First call hangs and is killed on timeout (short timeout, hang injected).
    monkeypatch.setenv("TEST_MARKITDOWN_HANG", "1")
    monkeypatch.setattr(ext_mod, "_EXTRACTION_DOC_TIMEOUT", 0.01)
    hung = _extract_document(b"%PDF-1.4 fake", "http://example.com/hang.pdf")
    assert hung.text is None
    monkeypatch.delenv("TEST_MARKITDOWN_HANG", raising=False)

    # Second call (same process / shared semaphore) must succeed. Use a normal
    # timeout so the worker subprocess has time to spawn + import on spawn
    # platforms (macOS) -- the point is that the killed first worker did not
    # stall the bounded extraction concurrency.
    monkeypatch.setattr(ext_mod, "_EXTRACTION_DOC_TIMEOUT", 10.0)
    monkeypatch.setenv("TEST_MARKITDOWN_OUTPUT", "## Recovered")
    recovered = _extract_document(b"%PDF-1.4 fake", "http://example.com/ok.pdf")
    assert recovered.markdown == "## Recovered"
    monkeypatch.delenv("TEST_MARKITDOWN_OUTPUT", raising=False)
