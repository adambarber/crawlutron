"""Document model: text parsing, bytearray paths and document-level extraction edges."""

import socket
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlparse

import httpx

from app.transport.document import (
    _extract_pdf_bytes_to_markdown,
    default_document_text_extractor,
    format_table,
)
from tests._extraction_helpers import format_extracted_document_text
from tests._fetcher_factories import run_http_strategy, scripted_fetcher
from tests._policy_factories import canonical

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
        md = format_table([["Name", "Department", "Salary"], ["Alice", "Engineering", "120000"]])
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

    async def test_default_document_text_extractor_fetches_real_pdf_url(self, base_url):
        url = f"{base_url}/sample_table.pdf"
        md = default_document_text_extractor(canonical(url))
        assert "Alice" in md
        assert "Department" in md
        assert "|" in md


class TestDocumentRedirectSsrf:
    """A public PDF that redirects to an internal host must not be followed.

    The extractor connects to the PINNED IP returned by validation, so
    urllib never re-resolves DNS at connect time -- the rebinding TOCTOU is
    closed. The logical hostname rides the Host header, which is what the
    fake opener dispatches on (both test hosts resolve to the same IP).
    """

    PDF_PATH = "http://initial.test/doc.pdf"
    EVIL_PATH = "http://evil.test/internal.pdf"
    ALLOWED_PATH = "http://allowed.test/doc.pdf"
    PINNED_IP = "93.184.216.1"

    def _install_fakes(self, monkeypatch, pdf_bytes, redirects):
        from urllib.parse import urlparse

        import app.transport.http_client_pool as pool_mod
        from tests._transport_doubles import MockResponse, dispatching_transport_client

        table = {"evil.test": "169.254.169.254"}

        def fake_getaddrinfo(hostname, port, *args, **kwargs):
            """The DNS table for this scenario, one level below the module.

            Stubbing the resolver rather than the module's ``_resolve_ips`` keeps
            the cache policy under test: the pinning path resolves fresh, so
            ``conftest`` clearing the cache between tests is enough for a stub
            here to be the whole of DNS.
            """
            if ":" in hostname:
                return []
            if hostname in (self.PINNED_IP, "169.254.169.254"):
                # That is what makes the logical-vs-connection URL split observable.
                raise socket.gaierror(8, "nodename nor servname provided")
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (table.get(hostname, self.PINNED_IP), port))]

        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
        monkeypatch.setenv("CRAWLUTRON_ALLOW_PRIVATE_TARGETS", "0")

        fetched: list[str] = []  # logical host names from the Host header
        connect_urls: list[str] = []  # URLs actually handed to the connection

        def handler(request: httpx.Request) -> httpx.Response:
            connect_urls.append(str(request.url))
            host = request.headers.get("Host", "")
            fetched.append(host)
            location = redirects.get(f"{host}{urlparse(str(request.url)).path}")
            if location is not None:
                # Httpx hands a 30x back as a response rather than raising, and the shared chain reads the
                # Location off it -- the opposite of urllib, where a redirect surfaced as HTTPError.
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
        markdown = default_document_text_extractor(canonical(url or self.PDF_PATH))
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
        """The connection URL carries the validated IP, not the hostname.

        urllib never re-resolves because the request URL host IS the IP that
        passed the SSRF check; the original hostname rides the Host header.
        """
        connect_urls = self._drive_extraction(monkeypatch).connect_urls
        assert connect_urls, "extractor must issue a pinned connect"
        assert urlparse(connect_urls[0]).hostname == self.PINNED_IP

    def test_https_document_connects_by_hostname_not_ip(self, monkeypatch):
        """-A: https keeps the hostname (certs carry no IP SANs) -- the
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
        """-B: hops 2+ re-validate and re-pin. A hop whose resolution is
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
        assert "hop1.test" in run.fetched
        assert "evil.test" not in run.fetched

    def test_relative_redirect_resolves_against_hostname_url(self, monkeypatch):
        """-B: the next Location urljoins against the LOGICAL (hostname)
        URL, never the pinned-IP connect URL -- the resolver must see the
        hostname, and a resolver that refuses IP literals proves which one
        the code actually used."""
        run = self._drive_extraction(monkeypatch, {"initial.test/doc.pdf": "/doc2.pdf"})
        assert "Alice" in run.markdown
        assert "initial.test" in run.fetched


async def test_document_extraction_with_uncompressed_bytearray_body(base_url):
    """Simulate fetcher delivering CSV body as bytearray (no content-encoding)."""
    # csv served WITHOUT compression -> decompress_body returns the raw buffer unchanged, which is a bytearray
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
    def test_table_empty(self):
        assert format_table([]) == ""

    def test_extract_unreachable_url(self):
        assert default_document_text_extractor(canonical("http://127.0.0.1:1/nowhere.pdf")) == ""

    def test_extract_broken_pdf_from_server(self, base_url):
        text = default_document_text_extractor(canonical(base_url + "/broken.pdf"))
        assert text == ""

    def test_oversized_pdf_body_yields_empty_text(self, base_url):
        # The read itself is capped.
        import app.extraction.content as extraction_mod

        original_cap = extraction_mod.EXTRACTION_DOC_MAX_INPUT_BYTES
        extraction_mod.EXTRACTION_DOC_MAX_INPUT_BYTES = 64
        try:
            text = default_document_text_extractor(canonical(base_url + "/sample_table.pdf"))
        finally:
            extraction_mod.EXTRACTION_DOC_MAX_INPUT_BYTES = original_cap
        assert text == ""


class TestDocumentTextParserEdges:
    def test_range_action(self):
        out = format_extracted_document_text(
            "1a - 1c Approve\n1a.\nBuild the thing\n1b.\nFix the bug\n1c.\nShip it",
            source_url="http://x/",
        )
        assert "Action: Approve" in out

    def test_action(self):
        out = format_extracted_document_text(
            "2a Approve all\n2a.\nDetail line",
            source_url="http://x/",
        )
        assert "Action: Approve all" in out

    def test_section_title(self):
        out = format_extracted_document_text(
            "1.\nSection Title\nBody text",
            source_url="http://x/",
        )
        assert "1. Section Title" in out

    def test_item_numbered_continuation(self):
        out = format_extracted_document_text(
            "3a.\n3. Build the thing\n4. Fix it",
            source_url="http://x/",
        )
        assert "3a. Build the thing" in out

    def test_non_item_section_skipped(self):
        out = format_extracted_document_text(
            "PUBLIC PARTICIPATION\nWe welcome comments",
            source_url="http://x/",
        )
        assert "PUBLIC PARTICIPATION" not in out

    def test_prose_merge(self):
        out = format_extracted_document_text(
            "First sentence here.\nSecond sentence here.",
            source_url="http://x/",
        )
        assert "First sentence here." in out
        assert "Second sentence here." in out

    def test_heading_join_skipped(self):
        # A heading must not absorb the paragraph under it. Asserting that the word survives proves nothing --
        # it survives either way -- so the assertion has to be about the record boundary between the two.
        out = format_extracted_document_text(
            "INTRODUCTION\nBody continues here",
            source_url="http://x/",
        )
        assert out == "INTRODUCTION\n\nBody continues here"

    def test_long_sentence_is_not_a_heading_and_so_does_join(self):
        # The other side of the same rule: all-caps-and-short is the test, so a long mixed-case sentence is
        # prose and merges with what follows it.
        out = format_extracted_document_text(
            "a normal sentence that is far too long to be a heading\nand it continues here",
            source_url="http://x/",
        )
        assert out == "a normal sentence that is far too long to be a heading and it continues here"

    def test_comma_join(self):
        out = format_extracted_document_text(
            "First clause here.\n, and more detail",
            source_url="http://x/",
        )
        assert ", and more detail" in out

    def test_all_blank_input_yields_no_records(self):
        # The line scan runs off the end rather than stopping at a blank line, and a document that is nothing
        # but blanks produces nothing.
        assert format_extracted_document_text("\n\n   \n", source_url="http://x/") == ""

    def test_item_marker_is_not_absorbed_as_an_item_title(self):
        # Both title pickers have to apply the same exclusions.
        out = format_extracted_document_text("1a.\nBuild the thing\n1.\n1b.\nShip it", source_url="http://x/")
        assert out == "1a. Build the thing\n\n1.\n\n1b. Ship it"

    def test_section_header_over_an_item_keeps_both_records(self):
        # The neighbouring shape, for contrast: the header declines the marker as its title, so the marker
        # becomes the sibling item's own record instead.
        out = format_extracted_document_text("1.\n1a.\nBuild the thing", source_url="http://x/")
        assert out == "1.\n\n1a. Build the thing"

    def test_non_item_section_is_not_read_as_a_section_title(self):
        out = format_extracted_document_text("1.\nPublic Participation\nBuild it", source_url="http://x/")
        assert out == "1.\n\nBuild it"

    def test_non_item_section_dropped_and_ordinary_text_kept(self):
        out = format_extracted_document_text(
            "Normal text\nPUBLIC PARTICIPATION\nItem one", source_url="http://x/"
        )
        assert "PUBLIC PARTICIPATION" not in out
        assert "Normal text" in out
        assert "Item one" in out

    def test_item_marker_after_a_section_header_is_not_read_as_its_title(self):
        # "1." followed by "1a." must not collapse into a section titled "1a."; the marker stays its own
        # record and the header keeps its title.
        marked = format_extracted_document_text("1.\n1a.\nApprove the plan", source_url="http://x/")
        assert marked == "1.\n\n1a. Approve the plan"
        # The same header over an ordinary line does take it as a title, which is what makes the marker case a
        # decision rather than a formatting accident.
        assert (
            format_extracted_document_text("1.\nApprove the plan", source_url="http://x/")
            == "1. Approve the plan"
        )

    def test_numbered_continuation_inside_an_item_title_loses_its_number(self):
        out = format_extracted_document_text("1a.\nBuild\n4. Fix it", source_url="http://x/")
        assert out == "1a. Build Fix it"

    def test_section_header_inside_an_item_title_is_absorbed_and_scanning_continues(self):
        out = format_extracted_document_text("1a.\nBuild the thing\n1.\nNext section", source_url="http://x/")
        assert out == "1a. Build the thing Next section"

    def test_blank_line_skipped_in_formatter(self):
        out = format_extracted_document_text("Intro paragraph.\n\nSecond paragraph.", source_url="http://x/")
        assert "Intro paragraph." in out
        assert "Second paragraph." in out

    def test_blank_line_inside_an_item_title_is_skipped(self):
        out = format_extracted_document_text("1a.\nBuild\n\nMore detail", source_url="http://x/")
        assert out == "1a. Build More detail"
