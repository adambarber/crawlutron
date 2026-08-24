import time

import pytest

import app.crawl.extraction as ext_mod
from app.crawl.extraction import _extract_document, _EXTRACTION_DOC_TIMEOUT, _MAX_EXTRACTED_MARKDOWN


@pytest.fixture(autouse=True)
def _reset_doc_extraction_pool():
    """Reset the markitdown process pool around each test in this module.

    These tests inject env vars (TEST_MARKITDOWN_HANG / TEST_MARKITDOWN_OUTPUT)
    that pool workers inherit at spawn, so every test needs a fresh pool. This
    is deliberately scoped here instead of an autouse conftest fixture: tearing
    the pool down for every test in the suite made it dramatically slower.
    """
    if ext_mod._DOC_EXTRACTION_EXECUTOR is not None:
        ext_mod._DOC_EXTRACTION_EXECUTOR.shutdown(wait=False)
        ext_mod._DOC_EXTRACTION_EXECUTOR = None
    yield
    if ext_mod._DOC_EXTRACTION_EXECUTOR is not None:
        ext_mod._DOC_EXTRACTION_EXECUTOR.shutdown(wait=False)
        ext_mod._DOC_EXTRACTION_EXECUTOR = None


async def test_extract_document_timeout_is_enforced(monkeypatch, real_sleeps):
    """A document extraction exceeding the wall-clock timeout returns None (F5).

    Uses ``real_sleeps`` because the slow extraction is simulated with a real
    ``time.sleep`` which the autouse ``fast_sleeps`` no-ops.
    """
    # Use env var to signal test hang to subprocess (inherited by process pool)
    monkeypatch.setenv("TEST_MARKITDOWN_HANG", "1")
    # Set short timeout - patch the module variable directly
    monkeypatch.setattr(ext_mod, "_EXTRACTION_DOC_TIMEOUT", 0.01)
    # Reset the process pool to pick up new timeout
    ext_mod._DOC_EXTRACTION_EXECUTOR = None

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

    # Use env var to inject test data into subprocess
    monkeypatch.setenv("TEST_MARKITDOWN_OUTPUT", huge)
    # Set the cap directly
    monkeypatch.setattr(ext_mod, "_MAX_EXTRACTED_MARKDOWN", test_cap)
    # Reset the process pool
    ext_mod._DOC_EXTRACTION_EXECUTOR = None

    result = _extract_document(b"%PDF-1.4 fake", "http://example.com/x.pdf")
    assert result.markdown is not None
    assert len(result.markdown) == test_cap

    monkeypatch.delenv("TEST_MARKITDOWN_OUTPUT", raising=False)


async def test_extract_document_normal_works(monkeypatch):
    """Normal extraction returns the markdown content."""
    # Use env var to inject test data into subprocess
    monkeypatch.setenv("TEST_MARKITDOWN_OUTPUT", "## Hello Markdown")
    # Reset the process pool
    ext_mod._DOC_EXTRACTION_EXECUTOR = None

    result = _extract_document(b"%PDF-1.4 fake", "http://example.com/x.pdf")
    assert result.markdown == "## Hello Markdown"
    assert result.text == "## Hello Markdown"

    monkeypatch.delenv("TEST_MARKITDOWN_OUTPUT", raising=False)


# ============================================================
# Finding 2: Document extraction uses process pool for kill-on-timeout
# ============================================================

import os

async def test_extract_document_process_pool_isolation(monkeypatch, real_sleeps):
    """Document extraction runs in a separate process so a hang can be killed
    and does not degrade the shared thread pool.
    """
    # Use a very short timeout - patch the module variable directly
    monkeypatch.setattr(ext_mod, "_EXTRACTION_DOC_TIMEOUT", 0.01)
    # Reset the process pool
    ext_mod._DOC_EXTRACTION_EXECUTOR = None
    from app.crawl.extraction import _EXTRACTION_DOC_TIMEOUT

    assert _EXTRACTION_DOC_TIMEOUT == 0.01

    # The subprocess will check this env var and hang if set
    monkeypatch.setenv("TEST_MARKITDOWN_HANG", "1")

    result = _extract_document(b"%PDF-1.4 fake", "http://example.com/x.pdf")
    # Should return None due to timeout (process killed)
    assert result.text is None
    assert result.markdown is None

    # Clean up env var for other tests
    monkeypatch.delenv("TEST_MARKITDOWN_HANG", raising=False)


async def test_extract_document_process_pool_normal_works(monkeypatch):
    """Normal document extraction still works with process pool."""
    # Use normal timeout - patch the module variable directly
    monkeypatch.setattr(ext_mod, "_EXTRACTION_DOC_TIMEOUT", 10.0)
    # Reset the process pool
    ext_mod._DOC_EXTRACTION_EXECUTOR = None
    from app.crawl.extraction import _extract_document

    # Don't set TEST_MARKITDOWN_HANG - normal operation
    result = _extract_document(b"%PDF-1.4 fake", "http://example.com/x.pdf")
    # With fake bytes, markitdown returns something (or None for unparseable)
    # The key is it doesn't crash
    assert isinstance(result, ext_mod.ExtractedContent)
