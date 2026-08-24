"""PDF/Office extraction behavior with a real PDF fixture (no mocks)."""

from pathlib import Path

from app.crawl.strategies.document import (
    _default_document_text_extractor,
    _extract_pdf_bytes_to_markdown,
    _format_table,
    _pdf_to_markdown,
)

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "sample_table.pdf"


class TestPdfTablePreservation:
    def test_format_table_renders_markdown(self):
        md = _format_table([["Name", "Department", "Salary"], ["Alice", "Engineering", "120000"]])
        assert "Name" in md
        assert "Department" in md
        assert "Alice" in md
        assert "Engineering" in md

    def test_pdf_bytes_to_markdown_extracts_table(self):
        pdf_bytes = FIXTURE.read_bytes()
        md = _extract_pdf_bytes_to_markdown(pdf_bytes)
        assert "Name" in md
        assert "Salary" in md
        assert "Alice" in md
        assert "Engineering" in md
        assert "|" in md

    def test_pdf_to_markdown_with_real_pdfplumber(self):
        import pdfplumber

        with pdfplumber.open(FIXTURE) as pdf:
            md = _pdf_to_markdown(pdf)
        assert "Alice" in md
        assert "|" in md

    async def test_default_document_text_extractor_fetches_real_pdf_url(self, base_url):
        url = f"{base_url}/sample_table.pdf"
        md = _default_document_text_extractor(url)
        assert "Alice" in md
        assert "Department" in md
        assert "|" in md
