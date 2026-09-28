"""Document model: text parsing, bytearray paths and document-level extraction edges."""

from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlparse

from app.crawl.strategies.document import (
    _extract_pdf_bytes_to_markdown,
    _format_table,
    _pdf_to_markdown,
)
from tests._extraction_helpers import (
    _collect_document_item_lines,
    _default_document_text_extractor,
    _document_item_range,
    _format_extracted_document_text,
    _is_document_item_marker,
    _is_document_non_item_section,
    _join_document_paragraph_lines,
    _looks_like_document_heading,
    _next_non_empty_document_line,
    _should_join_document_paragraph,
)
from tests._fetcher_factories import run_http_strategy, scripted_fetcher

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "sample_table.pdf"


class _ExtractionRun(NamedTuple):
    """What one extractor run did, as observed through the fake opener."""

    fetched: list[str]
    """Logical host names, from the ``Host`` header of each request."""
    connect_urls: list[str]
    """The URLs actually handed to the connection (the pinned-IP side)."""
    markdown: str
    """Whatever the extractor managed to extract; empty when it refused to."""


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
        from urllib.parse import urlparse

        import httpx

        import app.crawl.strategies.http_client_pool as pool_mod
        import app.crawl.strategies.pinned_transport as http_strategy
        from tests._transport_doubles import MockResponse, dispatching_transport_client

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

        def handler(request: httpx.Request) -> httpx.Response:
            connect_urls.append(str(request.url))
            host = request.headers.get("Host", "")
            fetched.append(host)
            location = redirects.get(f"{host}{urlparse(str(request.url)).path}")
            if location is not None:
                # httpx hands a 30x back as a response rather than raising, and
                # the shared chain reads the Location off it -- the opposite of
                # urllib, where a redirect surfaced as HTTPError. Driving the
                # real client is what makes that difference observable.
                return MockResponse(302, {"Location": location})
            return MockResponse(200, content=pdf_bytes)

        client = dispatching_transport_client(handler)
        monkeypatch.setattr(pool_mod, "get_async_http_client", lambda: client)
        return fetched, connect_urls

    def _drive_extraction(self, monkeypatch, redirects=None, url=None) -> _ExtractionRun:
        """Install the fakes, run the real extractor over the fixture, report the run.

        The pinning questions in this class are all questions about *where* the
        extractor connected -- the logical ``Host`` it sent, the URL it handed
        to the connection, and what it managed to read back. Staging the fakes
        and driving the extractor is one step, so it is written once here and a
        test states only the question it has of the run.
        """
        fetched, connect_urls = self._install_fakes(monkeypatch, FIXTURE.read_bytes(), redirects or {})
        markdown = _default_document_text_extractor(url or self.PDF_PATH)
        return _ExtractionRun(fetched, connect_urls, markdown)

    def test_redirect_to_internal_host_is_not_followed(self, monkeypatch):
        run = self._drive_extraction(monkeypatch, {"initial.test/doc.pdf": self.EVIL_PATH})
        assert run.markdown == ""
        assert "initial.test" in run.fetched  # the pinned connect happened
        assert "evil.test" not in run.fetched  # the hop was rejected before connect

    def test_redirect_to_public_host_still_works(self, monkeypatch):
        run = self._drive_extraction(monkeypatch, {"initial.test/doc.pdf": self.ALLOWED_PATH})
        assert "Alice" in run.markdown
        assert "allowed.test" in run.fetched

    def test_connect_is_pinned_to_validated_ip(self, monkeypatch):
        """H5: the connection URL carries the validated IP, not the hostname.

        urllib never re-resolves because the request URL host IS the IP that
        passed the SSRF check; the original hostname rides the Host header.
        """
        connect_urls = self._drive_extraction(monkeypatch).connect_urls
        assert connect_urls, "extractor must issue a pinned connect"
        assert urlparse(connect_urls[0]).hostname == self.PINNED_IP

    def test_https_document_connects_by_hostname_not_ip(self, monkeypatch):
        """H5-A: https keeps the hostname (certs carry no IP SANs) -- the
        pinned IP must never surface in an https request URL, or every properly
        configured site would fail TLS and silently break extraction.

        The assertion is about the https HOP, not every hop. The redirect target
        here is an http URL, so the second hop is pinned like any other http
        fetch and carries the IP; a blanket "no URL anywhere is an IP" would be
        asserting the opposite of the pin rule.
        """
        run = self._drive_extraction(
            monkeypatch,
            {"initial.test/doc.pdf": self.ALLOWED_PATH},
            url="https://initial.test/doc.pdf",
        )
        assert "Alice" in run.markdown
        assert run.connect_urls
        https_hop = urlparse(run.connect_urls[0])
        assert https_hop.scheme == "https"
        assert https_hop.hostname == "initial.test", "SNI needs the logical hostname"
        assert https_hop.hostname != self.PINNED_IP, "the pinned IP must not surface in an https URL"

    def test_second_hop_dns_rebind_aborts(self, monkeypatch):
        """H5-B: hops 2+ re-validate and re-pin. A hop whose resolution is
        link-local must abort the extraction, closing the rebind window that
        would otherwise reopen on every hop after the first."""
        run = self._drive_extraction(
            monkeypatch,
            {
                "initial.test/doc.pdf": "http://hop1.test/one.pdf",
                "hop1.test/one.pdf": self.EVIL_PATH,
            },
        )
        assert run.markdown == ""
        # The walk did advance exactly one hop before the second was refused.
        # Without this the assertion below would also hold if nothing had been
        # fetched at all, which is exactly what a broken walk looks like.
        assert "hop1.test" in run.fetched
        assert "evil.test" not in run.fetched

    def test_relative_redirect_resolves_against_hostname_url(self, monkeypatch):
        """H5-B: the next Location urljoins against the LOGICAL (hostname)
        URL, never the pinned-IP connect URL -- the resolver must see the
        hostname, and a resolver that refuses IP literals proves which one
        the code actually used."""
        run = self._drive_extraction(monkeypatch, {"initial.test/doc.pdf": "/doc2.pdf"})
        assert "Alice" in run.markdown
        assert "initial.test" in run.fetched


async def test_document_extraction_with_uncompressed_bytearray_body(base_url):
    """Simulate fetcher delivering CSV body as bytearray (no content-encoding)."""
    # csv served WITHOUT compression -> decompress_body returns the raw
    # buffer unchanged, which is a bytearray
    fetcher = scripted_fetcher(status=200, ctype="text/csv", body=bytearray(b"name,age\nAlice,30\nBob,25"))

    result = await run_http_strategy(
        url="https://sudbury.ma.us/x.csv",
        policy={
            "strategies": ["HTTP_DECLARED_CRAWLER"],
            "limits": {"timeout_seconds": 10},
            "headers": {},
            "extraction": "document",
        },
        http_fetcher=fetcher,
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
