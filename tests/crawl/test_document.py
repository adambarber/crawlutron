"""PDF/Office extraction behavior with a real PDF fixture (no mocks)."""

import unittest
from pathlib import Path

from app.crawl.strategies.document import (
    _default_document_text_extractor,
    _extract_pdf_bytes_to_markdown,
    _format_table,
    _pdf_to_markdown,
)
from helpers.server import spawn_server

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "sample_table.pdf"


class PdfTablePreservationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = spawn_server()
        cls.base = cls.server.base_url

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()

    def test_format_table_renders_markdown(self) -> None:
        md = _format_table([["Name", "Department", "Salary"], ["Alice", "Engineering", "120000"]])
        self.assertIn("Name", md)
        self.assertIn("Department", md)
        self.assertIn("Alice", md)
        self.assertIn("Engineering", md)

    def test_pdf_bytes_to_markdown_extracts_table(self) -> None:
        with open(FIXTURE, "rb") as handle:
            pdf_bytes = handle.read()
        md = _extract_pdf_bytes_to_markdown(pdf_bytes)
        self.assertIn("Name", md)
        self.assertIn("Salary", md)
        self.assertIn("Alice", md)
        self.assertIn("Engineering", md)
        self.assertIn("|", md)

    def test_pdf_to_markdown_with_real_pdfplumber(self) -> None:
        import pdfplumber

        with pdfplumber.open(FIXTURE) as pdf:
            md = _pdf_to_markdown(pdf)
        self.assertIn("Alice", md)
        self.assertIn("|", md)

    def test_default_document_text_extractor_fetches_real_pdf_url(self) -> None:
        url = f"{self.base}/sample_table.pdf"
        md = _default_document_text_extractor(url)
        self.assertIn("Alice", md)
        self.assertIn("Department", md)
        self.assertIn("|", md)


if __name__ == "__main__":
    unittest.main()
