"""Content extraction pipeline + Katana-inspired discovery (js_endpoints, pagination)."""

import asyncio

from app.crawl.extraction import (
    ExtractedContent,
    _detect_pagination,
    _extract_html,
    _is_html,
    _looks_like_document,
    _text_from_html,
    extract_content,
    extract_js_endpoints,
)


class TestExtractContent:
    def test_html_uses_trafilatura_markdown(self):
        html = "<html><body><main>Main content here</main></body></html>"
        result = extract_content(html, "text/html", "http://example.com/")
        assert isinstance(result, ExtractedContent)
        assert "Main content here" in (result.markdown or "")
        assert result.text == result.markdown

    def test_pdf_routes_to_markitdown_fallback(self):
        result = extract_content(b"%PDF-1.7 fake bytes", "application/pdf", "http://x/a.pdf")
        assert result.text == "%PDF-1.7 fake bytes"

    def test_csv_routes_to_markitdown_table(self):
        body = b"name,age\nAlice,30\nBob,25"
        result = extract_content(body, "text/csv", "http://x/data.csv")
        assert "Alice" in (result.text or "")
        assert "|" in (result.markdown or "")

    def test_json_passes_through_markitdown(self):
        body = b'{"a":1,"b":[2,3]}'
        result = extract_content(body, "application/json", "http://x/d.json")
        assert '"a":1' in (result.text or "")

    def test_plain_text_passes_through(self):
        result = extract_content("just some plain text", "text/plain", "http://x/note.txt")
        assert result.text == "just some plain text"
        assert result.markdown == "just some plain text"

    def test_doctype_without_content_type_routes_to_html(self):
        html = "<!doctype html><html><body>x</body></html>"
        result = extract_content(html, None, "http://x/")
        assert "x" in (result.markdown or "")

    def test_unrecognized_binary_passes_through_as_text(self):
        result = extract_content(b"\x00\x01binary", "application/octet-stream", "http://x/bin")
        assert result.text is not None

    def test_empty_or_unparseable_document_yields_no_content(self):
        result = extract_content(b"", "application/pdf", "http://x/e.pdf")
        assert result.text is None
        assert result.markdown is None


class TestRoutingHelpers:
    def test_is_html(self):
        assert _is_html("text/html", "<html></html>")
        assert _is_html("text/html; charset=utf-8", b"<html></html>")
        assert not _is_html("application/json", '{"a":1}')
        assert _is_html(None, "<!doctype html><html></html>")
        assert not _is_html(None, "plain")

    def test_looks_like_document(self):
        assert _looks_like_document("application/pdf", b"%PDF", "http://x/a.pdf")
        assert _looks_like_document("application/octet-stream", b"%PDF-1.4 data", "http://x/mystery")
        assert _looks_like_document("text/csv", b"a,b", "http://x/d.csv")
        assert _looks_like_document("application/json", b"{}", None)
        assert not _looks_like_document("text/html", b"<html>", "http://x/")
        assert not _looks_like_document("text/plain", b"hi", "http://x/note.txt")


class TestTextAndMetadata:
    def test_text_from_html_strips_scripts_and_tags(self):
        html = (
            "<html><body><p>Hello <b>World</b></p>"
            "<script>ignore_me();</script><style>.x{color:red}</style>"
            "</body></html>"
        )
        assert _text_from_html(html) == "Hello World"


class TestKatanaDiscovery:
    def test_extract_js_endpoints_finds_fetch_call(self):
        html = '<script>fetch("/api/data").then(r => r.json())</script>'
        endpoints = extract_js_endpoints(html)
        assert {"url": "/api/data"} in endpoints

    def test_extract_js_endpoints_finds_axios_and_xhr(self):
        html = (
            'axios.get("/api/items");'
            'xhr.open("GET", "/api/widgets");'
        )
        endpoints = extract_js_endpoints(html)
        assert {"url": "/api/items"} in endpoints
        assert {"url": "/api/widgets"} in endpoints

    def test_extract_js_endpoints_dedupes(self):
        html = 'fetch("/api/x"); fetch("/api/x")'
        endpoints = extract_js_endpoints(html)
        assert endpoints == [{"url": "/api/x"}]

    def test_extract_js_endpoints_resolves_against_source(self):
        html = 'fetch("/api/x"); axios.get("/y")'
        endpoints = extract_js_endpoints(html, "http://e.com/page")
        assert (
            endpoints
            == [{"url": "http://e.com/api/x"}, {"url": "http://e.com/y"}]
        )

    def test_extract_js_endpoints_dedupes_resolved(self):
        html = 'fetch("/api/x"); fetch("/api/x")'
        endpoints = extract_js_endpoints(html, "http://e.com/")
        assert endpoints == [{"url": "http://e.com/api/x"}]

    def test_extract_js_endpoints_unresolved_without_source(self):
        # Without a source URL the raw endpoint string is returned unresolved.
        html = 'fetch("/api/x")'
        endpoints = extract_js_endpoints(html)
        assert endpoints == [{"url": "/api/x"}]

    def test_extract_js_endpoints_empty_html(self):
        assert extract_js_endpoints("") == []
        assert extract_js_endpoints(None) == []  # type: ignore[arg-type]

    def test_extract_js_endpoints_skips_empty_endpoint(self):
        # A regex match with an empty/whitespace endpoint is skipped.
        endpoints = extract_js_endpoints('<script>fetch(" ")</script>', "http://e.com/")
        assert endpoints == []

    def test_extract_html_markdownify_fallback(self):
        # Trafilatura rejects an empty body; the markdownify fallback runs
        # (and may still yield nothing, falling through to plain-text).
        result = _extract_html("<html><body></body></html>", None)
        assert isinstance(result, ExtractedContent)
        assert result.markdown is None or isinstance(result.markdown, str)

    def test_detect_pagination_anchor_rel_next(self):
        html = '<a rel="next" href="/page2">Next</a>'
        assert _detect_pagination(html, "https://example.com/list") == "https://example.com/page2"

    def test_detect_pagination_link_rel_next(self):
        html = '<link rel="next" href="/p3">'
        assert _detect_pagination(html, "https://example.com/") == "https://example.com/p3"

    def test_detect_pagination_none_without_hint(self):
        html = '<a href="/page2">Next</a>'
        assert _detect_pagination(html, "https://example.com/") is None


async def test_extract_document_skips_oversized_input(monkeypatch):
    """#mem-guard: markitdown workers must never receive unbounded input."""
    import app.crawl.extraction as ex

    spawned = False
    class _FakeProcess:
        def __init__(self, *a, **k):
            nonlocal spawned
            spawned = True
        def start(self):
            pass
        def is_alive(self):
            return False
        def join(self, *a, **k):
            pass
        def terminate(self):
            pass
    monkeypatch.setattr(ex.multiprocessing, "Process", _FakeProcess)
    monkeypatch.setattr(ex, "_EXTRACTION_DOC_MAX_INPUT_BYTES", 100)

    out = await asyncio.to_thread(ex._extract_document, b"x" * 101, "http://x.test/big.pdf")
    assert out.text is None and out.markdown is None
    assert not spawned


async def test_extract_document_kill_switch(monkeypatch):
    """Env kill-switch lets deployments disable subprocess doc extraction."""
    import app.crawl.extraction as ex

    spawned = False
    class _FakeProcess:
        def __init__(self, *a, **k):
            nonlocal spawned
            spawned = True
        def start(self):
            pass
        def is_alive(self):
            return False
        def join(self, *a, **k):
            pass
        def terminate(self):
            pass
    monkeypatch.setattr(ex.multiprocessing, "Process", _FakeProcess)
    monkeypatch.setattr(ex, "_EXTRACTION_DOC_ENABLED", False)

    out = await asyncio.to_thread(ex._extract_document, b"tiny", None)
    assert out.text is None and out.markdown is None
    assert not spawned
