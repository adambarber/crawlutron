"""Document extraction edge cases: redirects, SSRF guards, timeouts."""

from _testkit import *


class TestDocumentExtractionEdges:
    def test_format_table_empty(self):
        assert _format_table([]) == ""

    def test_extract_unreachable_url(self):
        assert _default_document_text_extractor("http://127.0.0.1:1/nowhere.pdf") == ""

    def test_extract_broken_pdf_from_server(self, base_url):
        text = _default_document_text_extractor(base_url + "/broken.pdf")
        assert text == ""

    def test_oversized_pdf_body_yields_empty_text(self, base_url):
        # N2: the read itself is capped. /sample_table.pdf is a real PDF far
        # larger than the shrunken cap, so the extractor must fail closed
        # (empty text) instead of buffering the whole body.
        import app.crawl.extraction as extraction_mod

        original_cap = extraction_mod._EXTRACTION_DOC_MAX_INPUT_BYTES
        extraction_mod._EXTRACTION_DOC_MAX_INPUT_BYTES = 64
        try:
            text = _default_document_text_extractor(base_url + "/sample_table.pdf")
        finally:
            extraction_mod._EXTRACTION_DOC_MAX_INPUT_BYTES = original_cap
        assert text == ""
