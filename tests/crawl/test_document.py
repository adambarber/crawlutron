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


class _FakeHttpResponse:
    """Minimal urlopen-shaped response (context manager + read + status)."""

    def __init__(self, body: bytes, *, status: int = 200, headers=None):
        self._body = body
        self.status = status
        self.headers = headers or {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class TestDocumentRedirectSsrf:
    """H2: a public PDF that redirects to an internal host must not be followed."""

    PDF_PATH = "http://initial.test/doc.pdf"
    EVIL_PATH = "http://evil.test/internal.pdf"
    ALLOWED_PATH = "http://allowed.test/doc.pdf"

    def _install_fakes(self, monkeypatch, pdf_bytes, redirect_target):
        import urllib.request

        import app.crawl.strategies.http as http_strategy

        fetched: list[str] = []
        initial_path = self.PDF_PATH
        evil_path = self.EVIL_PATH
        allowed_path = self.ALLOWED_PATH

        def fake_resolve_ips(hostname, *, use_cache=True):
            if hostname == "evil.test":
                return ["169.254.169.254"]
            return ["93.184.216.1"]

        monkeypatch.setattr(http_strategy, "_resolve_ips", fake_resolve_ips)
        monkeypatch.setenv("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", "0")

        def fake_urlopen(url, timeout=30):
            # Emulate stdlib urlopen: follows redirects automatically.
            seen = 0
            while url == initial_path and seen < 5:
                fetched.append(url)
                url = redirect_target
                seen += 1
            fetched.append(url)
            if url == redirect_target and redirect_target == evil_path:
                return _FakeHttpResponse(pdf_bytes)
            if url == allowed_path:
                return _FakeHttpResponse(pdf_bytes)
            return _FakeHttpResponse(b"", status=302, headers={"Location": redirect_target})

        def fake_build_opener(*handlers):
            # Emulate a non-following opener: single-hop raw responses.
            class _Opener:
                def open(self, url, timeout=30):
                    fetched.append(url)
                    if url == initial_path:
                        return _FakeHttpResponse(b"", status=302, headers={"Location": redirect_target})
                    return _FakeHttpResponse(pdf_bytes)

            return _Opener()

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr(urllib.request, "build_opener", fake_build_opener)
        return fetched

    def test_redirect_to_internal_host_is_not_followed(self, monkeypatch):
        pdf_bytes = FIXTURE.read_bytes()
        fetched = self._install_fakes(monkeypatch, pdf_bytes, self.EVIL_PATH)
        assert _default_document_text_extractor(self.PDF_PATH) == ""
        assert self.EVIL_PATH not in fetched

    def test_redirect_to_public_host_still_works(self, monkeypatch):
        pdf_bytes = FIXTURE.read_bytes()
        fetched = self._install_fakes(monkeypatch, pdf_bytes, self.ALLOWED_PATH)
        md = _default_document_text_extractor(self.PDF_PATH)
        assert "Alice" in md
        assert self.ALLOWED_PATH in fetched
