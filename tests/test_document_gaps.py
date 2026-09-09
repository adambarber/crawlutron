"""TestDocumentGaps coverage tests (split from tests/test_coverage_gaps.py)."""

from coverage_helpers import *


class TestDocumentGaps:
    def test_format_table_empty(self):
        assert _format_table([]) == ""

    def test_extract_unreachable_url(self):
        assert _default_document_text_extractor("http://127.0.0.1:1/nowhere.pdf") == ""

    def test_extract_broken_pdf_from_server(self, base_url):
        text = _default_document_text_extractor(base_url + "/broken.pdf")
        assert text == ""
