"""Document model: text parsing, bytearray paths and document-level extraction edges."""

from pathlib import Path

from _testkit import *
from _testkit import make_fetch

from app.crawl.strategies.document import (
    _extract_pdf_bytes_to_markdown,
    _format_table,
    _pdf_to_markdown,
)
from app.validation import HostPolicy

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

    def read(self, amount: int = -1):
        # Real urllib responses accept an optional byte count (read_capped
        # asks for cap+1); mirror that contract.
        if amount == -1:
            return self._body
        return self._body[:amount]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class TestDocumentRedirectSsrf:
    """H2: a public PDF that redirects to an internal host must not be followed.

    H5: the extractor connects to the PINNED IP returned by validation, so
    urllib never re-resolves DNS at connect time -- the rebinding TOCTOU is
    closed. The logical hostname rides the Host header, which is what the
    fake opener dispatches on (both test hosts resolve to the same IP).
    """

    PDF_PATH = "http://initial.test/doc.pdf"
    EVIL_PATH = "http://evil.test/internal.pdf"
    ALLOWED_PATH = "http://allowed.test/doc.pdf"
    PINNED_IP = "93.184.216.1"

    def _install_fakes(self, monkeypatch, pdf_bytes, redirects):
        import socket
        import urllib.error
        import urllib.request
        from urllib.parse import urlparse

        import app.crawl.strategies.pinned_transport as http_strategy

        def fake_resolve_ips(hostname, *, use_cache=True):
            if ":" in hostname:
                return []
            if hostname in (self.PINNED_IP, "169.254.169.254"):
                # No DNS table entry for a bare IP literal: if the code ever
                # resolved a connect-URL host as if it were a hostname, the
                # hop must fail loudly instead of silently succeeding. That
                # is what makes the logical-vs-connection URL split
                # observable (H5-B).
                raise socket.gaierror(8, "nodename nor servname provided")
            if hostname == "evil.test":
                return ["169.254.169.254"]
            return [self.PINNED_IP]

        monkeypatch.setattr(http_strategy, "_resolve_ips", fake_resolve_ips)
        monkeypatch.setenv("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", "0")

        fetched: list[str] = []  # logical host names from the Host header
        connect_urls: list[str] = []  # URLs actually handed to the connection

        def fake_build_opener(*handlers):
            # Emulate the REAL non-following opener semantics: when
            # redirect_request returns None, urllib raises HTTPError -- it
            # never hands back a 30x response object (N2 regression guard).
            # Dispatch keys on the Host header: the connect URL carries the
            # pinned IP, so the logical hostname only exists there (H5-B).
            class _Opener:
                def open(self, request, timeout=30):
                    connect_urls.append(request.full_url)
                    host = request.get_header("Host") or ""
                    fetched.append(host)
                    location = redirects.get(f"{host}{urlparse(request.full_url).path}")
                    if location is not None:
                        raise urllib.error.HTTPError(
                            request.full_url,
                            302,
                            "Moved",
                            {"Location": location},
                            None,
                        )
                    return _FakeHttpResponse(pdf_bytes)

            return _Opener()

        monkeypatch.setattr(urllib.request, "build_opener", fake_build_opener)
        return fetched, connect_urls

    def test_redirect_to_internal_host_is_not_followed(self, monkeypatch):
        fetched, _ = self._install_fakes(
            monkeypatch, FIXTURE.read_bytes(), {"initial.test/doc.pdf": self.EVIL_PATH}
        )
        assert _default_document_text_extractor(self.PDF_PATH) == ""
        assert "initial.test" in fetched  # the pinned connect happened
        assert "evil.test" not in fetched  # the hop was rejected before connect

    def test_redirect_to_public_host_still_works(self, monkeypatch):
        fetched, _ = self._install_fakes(
            monkeypatch, FIXTURE.read_bytes(), {"initial.test/doc.pdf": self.ALLOWED_PATH}
        )
        md = _default_document_text_extractor(self.PDF_PATH)
        assert "Alice" in md
        assert "allowed.test" in fetched

    def test_connect_is_pinned_to_validated_ip(self, monkeypatch):
        """H5: the connection URL carries the validated IP, not the hostname.

        urllib never re-resolves because the request URL host IS the IP that
        passed the SSRF check; the original hostname rides the Host header.
        """
        from urllib.parse import urlparse

        _, connect_urls = self._install_fakes(monkeypatch, FIXTURE.read_bytes(), {})
        _default_document_text_extractor(self.PDF_PATH)
        assert connect_urls, "extractor must issue a pinned connect"
        assert urlparse(connect_urls[0]).hostname == self.PINNED_IP

    def test_https_document_connects_by_hostname_not_ip(self, monkeypatch):
        """H5-A: https keeps the hostname (certs carry no IP SANs) -- the
        pinned IP must never surface in the request URL, or every properly
        configured site would fail TLS and silently break extraction."""
        from urllib.parse import urlparse

        _, connect_urls = self._install_fakes(
            monkeypatch, FIXTURE.read_bytes(), {"initial.test/doc.pdf": self.ALLOWED_PATH}
        )
        md = _default_document_text_extractor("https://initial.test/doc.pdf")
        assert "Alice" in md
        assert connect_urls
        assert all(urlparse(u).scheme == "https" for u in connect_urls)
        assert all(urlparse(u).hostname != self.PINNED_IP for u in connect_urls)

    def test_second_hop_dns_rebind_aborts(self, monkeypatch):
        """H5-B: hops 2+ re-validate and re-pin. A hop whose resolution is
        link-local must abort the extraction, closing the rebind window that
        would otherwise reopen on every hop after the first."""
        redirects = {
            "initial.test/doc.pdf": "http://hop1.test/one.pdf",
            "hop1.test/one.pdf": self.EVIL_PATH,
        }
        fetched, _ = self._install_fakes(monkeypatch, FIXTURE.read_bytes(), redirects)
        assert _default_document_text_extractor(self.PDF_PATH) == ""
        assert "evil.test" not in fetched

    def test_relative_redirect_resolves_against_hostname_url(self, monkeypatch):
        """H5-B: the next Location urljoins against the LOGICAL (hostname)
        URL, never the pinned-IP connect URL -- the resolver must see the
        hostname, and a resolver that refuses IP literals proves which one
        the code actually used."""
        redirects = {"initial.test/doc.pdf": "/doc2.pdf"}
        fetched, _ = self._install_fakes(monkeypatch, FIXTURE.read_bytes(), redirects)
        md = _default_document_text_extractor(self.PDF_PATH)
        assert "Alice" in md
        assert "initial.test" in fetched


async def test_document_extraction_with_uncompressed_bytearray_body(base_url):
    """Simulate fetcher delivering CSV body as bytearray (no content-encoding)."""
    from app.crawl.strategies.http import _http_strategy_result

    async def fetcher(url, headers, timeout, max_content_length=50 * 1024 * 1024):
        # csv served WITHOUT compression -> decompress_body returns the raw
        # buffer unchanged, which is a bytearray
        return make_fetch(
            url=url,
            final_url=url,
            status=200,
            ctype="text/csv",
            body=bytearray(b"name,age\nAlice,30\nBob,25"),
        )

    result = await _http_strategy_result(
        original_url="https://sudbury.ma.us/x.csv",
        normalized_url="https://sudbury.ma.us/x.csv",
        strategy="HTTP_DECLARED_CRAWLER",
        policy={
            "strategies": ["HTTP_DECLARED_CRAWLER"],
            "limits": {"timeout_seconds": 10},
            "headers": {},
            "extraction": "document",
        },
        http_fetcher=fetcher,
        host_policy=HostPolicy.unrestricted(),
    )
    assert result.status == "success", (result.status, result.classification)
    assert "Alice" in (result.text or "")


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
