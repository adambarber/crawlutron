"""Document text parser edge cases: encoding, malformed HTML, empty documents."""

from _testkit import *


class TestDocumentTextParserEdges:
    def test_range_action(self):
        out = _format_extracted_document_text(
            "1a - 1c Approve\n1a.\nBuild the thing\n1b.\nFix the bug\n1c.\nShip it",
            source_url="http://x/",
        )
        assert "Action: Approve" in out

    def test_action(self):
        out = _format_extracted_document_text(
            "2a Approve all\n2a.\nDetail line",
            source_url="http://x/",
        )
        assert "Action: Approve all" in out

    def test_section_title(self):
        out = _format_extracted_document_text(
            "1.\nSection Title\nBody text",
            source_url="http://x/",
        )
        assert "1. Section Title" in out

    def test_item_numbered_continuation(self):
        out = _format_extracted_document_text(
            "3a.\n3. Build the thing\n4. Fix it",
            source_url="http://x/",
        )
        assert "3a. Build the thing" in out

    def test_non_item_section_skipped(self):
        out = _format_extracted_document_text(
            "PUBLIC PARTICIPATION\nWe welcome comments",
            source_url="http://x/",
        )
        assert "PUBLIC PARTICIPATION" not in out

    def test_prose_merge(self):
        out = _format_extracted_document_text(
            "First sentence here.\nSecond sentence here.",
            source_url="http://x/",
        )
        assert "First sentence here." in out
        assert "Second sentence here." in out

    def test_heading_join_skipped(self):
        out = _format_extracted_document_text(
            "INTRODUCTION\nBody continues here",
            source_url="http://x/",
        )
        assert "INTRODUCTION" in out

    def test_comma_join(self):
        out = _format_extracted_document_text(
            "First clause here.\n, and more detail",
            source_url="http://x/",
        )
        assert ", and more detail" in out

    def test_helpers_direct(self):
        assert _looks_like_document_heading("INTRODUCTION")
        assert not (_looks_like_document_heading("a normal sentence that is far too long to be a heading"))
        assert _is_document_item_marker("1a.")
        assert not (_is_document_item_marker("Section"))
        assert _is_document_non_item_section("PUBLIC PARTICIPATION")
        assert not (_is_document_non_item_section("Normal text"))
        assert list(_document_item_range("1a", "1c")) == ["1a", "1b", "1c"]
        assert _next_non_empty_document_line(["", "a", "b"], 0) == 1
        assert (_next_non_empty_document_line(["", ""], 0)) is None
        assert _join_document_paragraph_lines("a", "b") == "a b"
        assert _join_document_paragraph_lines("a", ", b") == "a, b"
        assert _should_join_document_paragraph("First clause here", "and continues the work")
        lines, nxt = _collect_document_item_lines(["Build", "4. Fix it"], 0)
        assert lines == ["Build", "Fix it"]
        assert nxt == 2

    def test_blank_line_skipped_in_formatter(self):
        out = _format_extracted_document_text("Intro paragraph.\n\nSecond paragraph.", source_url="http://x/")
        assert "Intro paragraph." in out
        assert "Second paragraph." in out

    def test_blank_line_skipped_in_collect(self):
        lines, nxt = _collect_document_item_lines(["Build", "", "More detail"], 0)
        assert lines == ["Build", "More detail"]
        assert nxt == 3
