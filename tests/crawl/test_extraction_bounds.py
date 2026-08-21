import time

import pytest

import app.crawl.extraction as ext_mod
from app.crawl.extraction import _extract_document, _EXTRACTION_DOC_TIMEOUT, _MAX_EXTRACTED_MARKDOWN


async def test_extract_document_timeout_is_enforced(monkeypatch):
    """A document extraction exceeding the wall-clock timeout returns None (F5)."""
    monkeypatch.setattr(ext_mod, "_EXTRACTION_DOC_TIMEOUT", 0.01)

    class _SlowMarkItDown:
        def convert_stream(self, stream):
            time.sleep(0.05)
            class Res:
                text_content = "should not appear"
            return Res()

    # markitdown is imported inside _extract_document; patch the module
    monkeypatch.setattr("markitdown.MarkItDown", _SlowMarkItDown)

    result = _extract_document(b"%PDF-1.4 fake", "http://example.com/x.pdf")
    assert result.text is None
    assert result.markdown is None


async def test_extract_document_output_capped_at_5mb(monkeypatch):
    """Extracted markdown is truncated to _MAX_EXTRACTED_MARKDOWN (F5)."""
    huge = "x" * (_MAX_EXTRACTED_MARKDOWN + 1000)

    class _HugeMarkItDown:
        def convert_stream(self, stream):
            class Res:
                text_content = huge
            return Res()

    monkeypatch.setattr("markitdown.MarkItDown", _HugeMarkItDown)

    result = _extract_document(b"%PDF-1.4 fake", "http://example.com/x.pdf")
    assert result.markdown is not None
    assert len(result.markdown) == _MAX_EXTRACTED_MARKDOWN


async def test_extract_document_normal_works(monkeypatch):
    """Normal extraction returns the markdown content."""
    class _OkMarkItDown:
        def convert_stream(self, stream):
            class Res:
                text_content = "## Hello Markdown"
            return Res()

    monkeypatch.setattr("markitdown.MarkItDown", _OkMarkItDown)

    result = _extract_document(b"%PDF-1.4 fake", "http://example.com/x.pdf")
    assert result.markdown == "## Hello Markdown"
    assert result.text == "## Hello Markdown"
