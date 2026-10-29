"""Content extraction: markdown/text extraction, selector timeouts, bounds and single-pass parsing."""

import asyncio

from crawlutron.extraction.content import (
    ExtractedContent,
    extract_html,
    is_html,
    looks_like_document,
    text_from_html,
)
from crawlutron.extraction.doc_pool import doc_task_hooks, install_doc_pool_hooks
from tests._extraction_helpers import (
    detect_pagination,
    doc_worker_acquisitions,
    extract_content,
    extract_js_endpoints,
)
from tests._policy_factories import canonical


def test_document_test_hooks_follow_the_injected_seam():
    for hooks in ({"die": True, "hang": True}, None):
        install_doc_pool_hooks(hooks)
        assert doc_task_hooks() is hooks
    install_doc_pool_hooks(None)


class TestExtractContent:
    def test_html_uses_trafilatura_markdown(self):
        html = "<html><body><main>Main content here</main></body></html>"
        result = extract_content(html, "text/html", canonical("http://example.com/"))
        assert isinstance(result, ExtractedContent)
        assert "Main content here" in (result.markdown or "")
        assert result.text == result.markdown

    def test_pdf_routes_to_markitdown_fallback(self):
        result = extract_content(b"%PDF-1.7 fake bytes", "application/pdf", canonical("http://x/a.pdf"))
        assert result.text == "%PDF-1.7 fake bytes"

    def test_csv_routes_to_markitdown_table(self):
        body = b"name,age\nAlice,30\nBob,25"
        result = extract_content(body, "text/csv", canonical("http://x/data.csv"))
        assert "Alice" in (result.text or "")
        assert "|" in (result.markdown or "")

    def test_json_passes_through_markitdown(self):
        body = b'{"a":1,"b":[2,3]}'
        result = extract_content(body, "application/json", canonical("http://x/d.json"))
        assert '"a":1' in (result.text or "")

    def test_plain_text_passes_through(self):
        result = extract_content("just some plain text", "text/plain", canonical("http://x/note.txt"))
        assert result.text == "just some plain text"
        assert result.markdown == "just some plain text"

    def test_doctype_without_content_type_routes_to_html(self):
        html = "<!doctype html><html><body>x</body></html>"
        result = extract_content(html, None, canonical("http://x/"))
        assert "x" in (result.markdown or "")

    def test_unrecognized_binary_passes_through_as_text(self):
        result = extract_content(b"\x00\x01binary", "application/octet-stream", canonical("http://x/bin"))
        assert result.text is not None

    def test_empty_or_unparseable_document_yields_no_content(self):
        result = extract_content(b"", "application/pdf", canonical("http://x/e.pdf"))
        assert result.text is None
        assert result.markdown is None


class TestRoutingHelpers:
    def test_is_html(self):
        assert is_html("text/html", "<html></html>")
        assert is_html("text/html; charset=utf-8", b"<html></html>")
        assert not is_html("application/json", '{"a":1}')
        assert is_html(None, "<!doctype html><html></html>")
        assert not is_html(None, "plain")

    def test_looks_like_document(self):
        assert looks_like_document("application/pdf", b"%PDF", canonical("http://x/a.pdf"))
        assert looks_like_document(
            "application/octet-stream", b"%PDF-1.4 data", canonical("http://x/mystery")
        )
        assert looks_like_document("text/csv", b"a,b", canonical("http://x/d.csv"))
        assert looks_like_document("application/json", b"{}", None)
        assert not looks_like_document("text/html", b"<html>", canonical("http://x/"))
        assert not looks_like_document("text/plain", b"hi", canonical("http://x/note.txt"))


class TestTextAndMetadata:
    def test_text_from_html_strips_scripts_and_tags(self):
        html = (
            "<html><body><p>Hello <b>World</b></p>"
            "<script>ignore_me();</script><style>.x{color:red}</style>"
            "</body></html>"
        )
        assert text_from_html(html) == "Hello World"


class TestKatanaDiscovery:
    def test_extract_js_endpoints_finds_fetch_call(self):
        html = '<script>fetch("/api/data").then(r => r.json())</script>'
        endpoints = extract_js_endpoints(html)
        assert {"url": "/api/data"} in endpoints

    def test_extract_js_endpoints_finds_axios_and_xhr(self):
        html = 'axios.get("/api/items");xhr.open("GET", "/api/widgets");'
        endpoints = extract_js_endpoints(html)
        assert {"url": "/api/items"} in endpoints
        assert {"url": "/api/widgets"} in endpoints

    def test_extract_js_endpoints_dedupes(self):
        html = 'fetch("/api/x"); fetch("/api/x")'
        endpoints = extract_js_endpoints(html)
        assert endpoints == [{"url": "/api/x"}]

    def test_extract_js_endpoints_resolves_against_source(self):
        html = 'fetch("/api/x"); axios.get("/y")'
        endpoints = extract_js_endpoints(html, canonical("http://e.com/page"))
        assert endpoints == [{"url": "http://e.com/api/x"}, {"url": "http://e.com/y"}]

    def test_extract_js_endpoints_dedupes_resolved(self):
        html = 'fetch("/api/x"); fetch("/api/x")'
        endpoints = extract_js_endpoints(html, canonical("http://e.com/"))
        assert endpoints == [{"url": "http://e.com/api/x"}]

    def test_extract_js_endpoints_unresolved_without_source(self):
        # Without a source URL the raw endpoint string is returned unresolved.
        html = 'fetch("/api/x")'
        endpoints = extract_js_endpoints(html)
        assert endpoints == [{"url": "/api/x"}]

    def test_extract_js_endpoints_empty_html(self):
        # The helper is a thin untyped wrapper that defaults host_policy, so None is a supported input rather
        # than a suppressed type error.
        assert extract_js_endpoints("") == []
        assert extract_js_endpoints(None) == []

    def test_extract_js_endpoints_skips_empty_endpoint(self):
        # A regex match with an empty/whitespace endpoint is skipped.
        endpoints = extract_js_endpoints('<script>fetch(" ")</script>', canonical("http://e.com/"))
        assert endpoints == []

    def test_extract_html_markdownify_fallback(self):
        # Trafilatura rejects an empty body; the markdownify fallback runs (and may still yield nothing,
        # falling through to plain-text).
        result = extract_html("<html><body></body></html>", None)
        assert isinstance(result, ExtractedContent)

    def test_detect_pagination_anchor_rel_next(self):
        html = '<a rel="next" href="/page2">Next</a>'
        assert detect_pagination(html, canonical("https://example.com/list")) == canonical(
            "https://example.com/page2"
        )

    def test_detect_pagination_link_rel_next(self):
        html = '<link rel="next" href="/p3">'
        assert detect_pagination(html, canonical("https://example.com/")) == canonical(
            "https://example.com/p3"
        )

    def test_detect_pagination_none_without_hint(self):
        html = '<a href="/page2">Next</a>'
        assert detect_pagination(html, canonical("https://example.com/")) is None

    def test_detect_pagination_drops_non_http_href(self):
        # The href is attacker-controlled HTML: a non-http(s) rel=next must not escape into next_page_url
        for bad in ("javascript:alert(1)", "data:text/html,<x>", "mailto:a@b.c"):
            html = f'<a rel="next" href="{bad}">next</a>'
            assert detect_pagination(html, canonical("https://example.com/list")) is None

    def test_detect_pagination_still_resolves_relative(self):
        # The egress filter must not break legitimate relative pagination.
        assert detect_pagination(
            '<a rel="next" href="/p2">n</a>', canonical("https://example.com/list")
        ) == canonical("https://example.com/p2")

    def test_detect_pagination_protocol_relative_resolves(self):
        # //evil.com/x resolves against the page URL to https, so it survives the scheme filter exactly like
        # the HTTP path already allowed.
        assert detect_pagination(
            '<a rel="next" href="//example.com/p2">n</a>', canonical("https://example.com/list")
        ) == canonical("https://example.com/p2")


async def test_extract_document_skips_oversized_input(monkeypatch):
    """#mem-guard: markitdown workers must never receive unbounded input."""
    import crawlutron.extraction.content as ex
    import crawlutron.extraction.documents as docex

    acquisitions = doc_worker_acquisitions(monkeypatch)
    monkeypatch.setattr(docex, "EXTRACTION_DOC_MAX_INPUT_BYTES", 100)

    out = await asyncio.to_thread(ex.extract_document, b"x" * 101, "http://x.test/big.pdf")
    assert out.text is None and out.markdown is None
    assert acquisitions == [], "a refused document still asked for a markitdown worker"
