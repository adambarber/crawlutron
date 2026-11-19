"""JS endpoints: fetch/axios/jQuery/XHR literals admitted, deduped, capped."""

from __future__ import annotations

from crawlutron_extract import ExtractLimits, extract_js_endpoints


def test_fetch_and_axios_literals_admitted():
    html = '<script>fetch("/api/a"); axios.post("http://example.com/api/b");</script>'
    out = extract_js_endpoints(html, "http://example.com/")
    assert [str(u) for u in out] == ["http://example.com/api/a", "http://example.com/api/b"]


def test_jquery_and_xhr_literals_admitted():
    html = "<script>$.ajax('/api/c'); xhr.open('GET', '/api/d');</script>"
    out = extract_js_endpoints(html, "http://example.com/")
    assert [str(u) for u in out] == ["http://example.com/api/c", "http://example.com/api/d"]


def test_template_literal_admitted():
    html = "<script>fetch(`/api/e`);</script>"
    out = extract_js_endpoints(html, "http://example.com/")
    assert [str(u) for u in out] == ["http://example.com/api/e"]


def test_url_in_comment_admitted_heuristic():
    html = "<script>// fetch('/api/f')</script>"
    out = extract_js_endpoints(html, "http://example.com/")
    assert [str(u) for u in out] == ["http://example.com/api/f"]


def test_javascript_url_refused():
    html = "<script>fetch('javascript:alert(1)');</script>"
    assert extract_js_endpoints(html, "http://example.com/") == []


def test_deduped_and_capped():
    html = "<script>" + "".join(f'fetch("/api/{i}");' for i in range(6)) + "</script>"
    out = extract_js_endpoints(html, "http://example.com/", limits=ExtractLimits(max_links=3))
    assert len(out) == 3


def test_empty_html_returns_empty():
    assert extract_js_endpoints("", "http://example.com/") == []


def test_none_source_url_returns_empty():
    assert extract_js_endpoints("<script>fetch('/api/a');</script>", None) == []
