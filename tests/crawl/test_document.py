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

        import app.crawl.strategies.http as http_strategy

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
