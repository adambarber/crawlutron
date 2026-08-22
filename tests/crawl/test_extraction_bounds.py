import time

import pytest

import app.crawl.extraction as ext_mod
from app.crawl.extraction import _extract_document, _EXTRACTION_DOC_TIMEOUT, _MAX_EXTRACTED_MARKDOWN


async def test_extract_document_timeout_is_enforced(monkeypatch, real_sleeps):
    """A document extraction exceeding the wall-clock timeout returns None (F5).

    Uses ``real_sleeps`` because the slow extraction is simulated with a real
    ``time.sleep`` which the autouse ``fast_sleeps`` no-ops.
    """
    # Use env var to signal test hang to subprocess (inherited by process pool)
    monkeypatch.setenv("TEST_MARKITDOWN_HANG", "1")
    # Set short timeout
    monkeypatch.setenv("BROWSER_CRAWLER_EXTRACTION_DOC_TIMEOUT", "0.01")

    # Need to reload module so timeout and env var are picked up
    import importlib
    import app.crawl.extraction as ext_mod_reload
    importlib.reload(ext_mod_reload)
    from app.crawl.extraction import _extract_document

    result = _extract_document(b"%PDF-1.4 fake", "http://example.com/x.pdf")
    assert result.text is None
    assert result.markdown is None

    # Clean up
    monkeypatch.delenv("TEST_MARKITDOWN_HANG", raising=False)
    monkeypatch.delenv("BROWSER_CRAWLER_EXTRACTION_DOC_TIMEOUT", raising=False)


async def test_extract_document_output_capped_at_5mb(monkeypatch):
    """Extracted markdown is truncated to _MAX_EXTRACTED_MARKDOWN (F5)."""
    # Use a smaller cap for faster test (10KB instead of 5MB)
    test_cap = 10000
    huge = "x" * (test_cap + 1000)

    # Use env var to inject test data into subprocess
    monkeypatch.setenv("TEST_MARKITDOWN_OUTPUT", huge)

    # Need to reload module AND create fresh process pool to pick up new behavior
    import importlib
    import app.crawl.extraction as ext_mod_reload
    importlib.reload(ext_mod_reload)
    # Also reset the process pool
    ext_mod_reload._DOC_EXTRACTION_EXECUTOR = None
    # Set the cap AFTER reload
    monkeypatch.setattr(ext_mod_reload, "_MAX_EXTRACTED_MARKDOWN", test_cap)
    from app.crawl.extraction import _extract_document

    result = _extract_document(b"%PDF-1.4 fake", "http://example.com/x.pdf")
    assert result.markdown is not None
    assert len(result.markdown) == test_cap

    monkeypatch.delenv("TEST_MARKITDOWN_OUTPUT", raising=False)


async def test_extract_document_normal_works(monkeypatch):
    """Normal extraction returns the markdown content."""
    # Use env var to inject test data into subprocess
    monkeypatch.setenv("TEST_MARKITDOWN_OUTPUT", "## Hello Markdown")

    # Need to reload module to pick up new behavior
    import importlib
    import app.crawl.extraction as ext_mod
    importlib.reload(ext_mod)
    from app.crawl.extraction import _extract_document

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
    # Use a very short timeout
    monkeypatch.setenv("BROWSER_CRAWLER_EXTRACTION_DOC_TIMEOUT", "0.01")
    # Reload the timeout value
    import importlib
    import app.crawl.extraction as ext_mod
    importlib.reload(ext_mod)
    from app.crawl.extraction import _extract_document, _EXTRACTION_DOC_TIMEOUT

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
    # Use normal timeout
    monkeypatch.setenv("BROWSER_CRAWLER_EXTRACTION_DOC_TIMEOUT", "10.0")
    import importlib
    import app.crawl.extraction as ext_mod
    importlib.reload(ext_mod)
    from app.crawl.extraction import _extract_document

    # Don't set TEST_MARKITDOWN_HANG - normal operation
    result = _extract_document(b"%PDF-1.4 fake", "http://example.com/x.pdf")
    # With fake bytes, markitdown returns something (or None for unparseable)
    # The key is it doesn't crash
    assert isinstance(result, ext_mod.ExtractedContent)
