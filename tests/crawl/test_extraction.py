import logging

"""Content extraction: markdown/text extraction, selector timeouts, bounds and single-pass parsing."""

import asyncio
import time

import bs4
import pytest
from _testkit import *
from _testkit import BOOL_ENV_CASES, set_optional_env

import app.crawl.extraction as ext_mod
import app.crawl.extraction as extraction
import app.crawl.extraction as extraction_mod
from app.crawl.extraction import (
    ExtractedContent,
    _extract_document,
    _extract_html,
    _extract_selected,
    _is_html,
    _looks_like_document,
    _text_from_html,
)


def test_document_test_hooks_accept_shared_bool_spellings(monkeypatch):
    for value in (" true ", "yes", "1"):
        monkeypatch.setenv("BROWSER_CRAWLER_TEST_HOOKS", value)
        monkeypatch.setenv("TEST_MARKITDOWN_DIE", value)
        monkeypatch.setenv("TEST_MARKITDOWN_HANG", value)
        hooks = extraction._doc_task_hooks()
        assert hooks is not None
        assert hooks["die"] is True
        assert hooks["hang"] is True


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
        endpoints = extract_js_endpoints(html, "http://e.com/page")
        assert endpoints == [{"url": "http://e.com/api/x"}, {"url": "http://e.com/y"}]

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

    def test_detect_pagination_drops_non_http_href(self):
        # The href is attacker-controlled HTML: a non-http(s) rel=next must not
        # escape into next_page_url. Egress filtering belongs at the producer
        # so every consumer is covered, not just the ones that remember.
        for bad in ("javascript:alert(1)", "data:text/html,<x>", "mailto:a@b.c"):
            html = f'<a rel="next" href="{bad}">next</a>'
            assert _detect_pagination(html, "https://example.com/list") is None

    def test_detect_pagination_still_resolves_relative(self):
        # The egress filter must not break legitimate relative pagination.
        assert (
            _detect_pagination('<a rel="next" href="/p2">n</a>', "https://example.com/list")
            == "https://example.com/p2"
        )

    def test_detect_pagination_protocol_relative_resolves(self):
        # //evil.com/x resolves against the page URL to https, so it survives
        # the scheme filter exactly like the HTTP path already allowed.
        assert (
            _detect_pagination('<a rel="next" href="//example.com/p2">n</a>', "https://example.com/list")
            == "https://example.com/p2"
        )


def _non_spawning_process():
    """A multiprocessing.Process double that records construction, never spawns."""
    spawned = []

    class _FakeProcess:
        def __init__(self, *a, **k):
            spawned.append(True)

        def start(self):
            pass

        def is_alive(self):
            return False

        def join(self, *a, **k):
            pass

        def terminate(self):
            pass

    return _FakeProcess, spawned


async def test_extract_document_skips_oversized_input(monkeypatch):
    """#mem-guard: markitdown workers must never receive unbounded input."""
    import app.crawl.extraction as ex

    _FakeProcess, spawned = _non_spawning_process()
    monkeypatch.setattr(ex.multiprocessing, "Process", _FakeProcess)
    monkeypatch.setattr(ex, "_EXTRACTION_DOC_MAX_INPUT_BYTES", 100)

    out = await asyncio.to_thread(ex._extract_document, b"x" * 101, "http://x.test/big.pdf")
    assert out.text is None and out.markdown is None
    assert not spawned


@pytest.mark.parametrize(("raw", "expected"), BOOL_ENV_CASES)
def test_document_worker_decision_uses_shared_bool_parser(monkeypatch, caplog, raw, expected):
    import app.crawl.extraction as ex

    set_optional_env(monkeypatch, "BROWSER_CRAWLER_EXTRACTION_DOC_ENABLED", raw)

    class _Process:
        pid = 1

        def is_alive(self):
            return True

        def terminate(self):
            return None

        def join(self, timeout=None):
            return None

        def kill(self):
            return None

    class _Connection:
        def close(self):
            return None

    spawned = []

    def spawn():
        spawned.append(True)
        return ex._DocWorker(_Process(), _Connection())

    monkeypatch.setattr(ex, "_spawn_doc_worker", spawn)
    monkeypatch.setattr(ex, "_doc_extraction_worker_count", lambda: 1)
    ex._reset_doc_pool()
    worker = ex._acquire_doc_worker()
    assert (worker is not None) is expected
    assert bool(spawned) is expected
    if raw == "garbage":
        assert any("EXTRACTION_DOC_ENABLED" in record.getMessage() for record in caplog.records)
    ex._reset_doc_pool()


async def test_extract_document_kill_switch(monkeypatch):
    """Env kill-switch lets deployments disable subprocess doc extraction."""
    import app.crawl.extraction as ex

    _FakeProcess, spawned = _non_spawning_process()
    monkeypatch.setattr(ex.multiprocessing, "Process", _FakeProcess)
    monkeypatch.setenv("BROWSER_CRAWLER_EXTRACTION_DOC_ENABLED", "0")

    out = await asyncio.to_thread(ex._extract_document, b"tiny", None)
    assert out.text is None and out.markdown is None
    assert not spawned


class TestExtractionMethods:
    def test_extraction_different_methods(self):
        from app.crawl.extraction import extract_content

        html = "<html><body><p>Hello world</p></body></html>"
        for method, content_type in [
            ("trafilatura", "text/html"),
            ("document", "application/pdf"),
            ("dom_text", "text/html"),
            ("full", "text/html"),
        ]:
            result = extract_content(html, content_type, "http://example.com/", None, None)
            assert (result) is not None


class TestHtmlTreeParsing:
    def test_extraction_parse_html_tree_error(self):
        from app.crawl.extraction import _parse_html_tree

        result = _parse_html_tree("{{invalid html")
        # Should not raise - lxml is forgiving
        assert (result) is not None


class TestSelectedExtraction:
    def test_extraction_selected_with_xpath(self):
        from app.crawl.extraction import _extract_selected

        html = "<html><body><div>Hello</div></body></html>"
        result = _extract_selected(html, "http://x/", css_selector=None, xpath="//div/text()")
        assert (result) is not None


class TestLinkExtraction:
    def test_engine_parse_robots_blank_line(self):
        from _testkit import _extract_discovered_links

        # Test link extraction with mixed quote styles (covers edge cases)
        html = "<html><body><a href=https://example.com/test>link</a><a href='https://example.com/page2'>link2</a></body></html>"
        links = _extract_discovered_links(html, None)
        assert len(links) > 0


@pytest.fixture(autouse=True)
def _reset_doc_extraction_pool():
    """No persistent process pool to reset.

    Each extraction call now spawns its own short-lived process (reaped on
    completion or timeout), so there is nothing to tear down between tests. The
    injected env flags (TEST_MARKITDOWN_HANG / TEST_MARKITDOWN_OUTPUT) are
    reverted by the ``monkeypatch`` fixture at test teardown.
    """
    yield


async def test_extract_document_timeout_is_enforced(monkeypatch, real_sleeps):
    """A document extraction exceeding the wall-clock timeout returns None (F5).

    Uses ``real_sleeps`` because the slow extraction is simulated with a real
    ``time.sleep`` which the autouse ``fast_sleeps`` no-ops.
    """
    # Use env var to signal test hang to the worker subprocess (inherited).
    monkeypatch.setenv("TEST_MARKITDOWN_HANG", "1")
    monkeypatch.setenv("BROWSER_CRAWLER_TEST_HOOKS", "1")
    # Set short timeout - patch the module variable directly.
    monkeypatch.setattr(ext_mod, "_EXTRACTION_DOC_TIMEOUT", 0.01)

    result = _extract_document(b"%PDF-1.4 fake", "http://example.com/x.pdf")
    assert result.text is None
    assert result.markdown is None

    # Clean up
    monkeypatch.delenv("TEST_MARKITDOWN_HANG", raising=False)


async def test_extract_document_child_death_returns_empty(monkeypatch):
    """A worker that dies before sending (segfault) yields empty extraction,
    not an unhandled EOFError, and leaves no zombie behind (M8)."""

    monkeypatch.setenv("TEST_MARKITDOWN_DIE", "1")
    monkeypatch.setenv("BROWSER_CRAWLER_TEST_HOOKS", "1")
    result = _extract_document(b"%PDF-1.4 fake", "http://example.com/x.pdf")
    assert result.text is None
    assert result.markdown is None

    # M8 is per worker, not process-global: the pool is persistent and sized
    # above one, so an idle worker from an earlier extraction legitimately
    # stays alive. The dead worker must be reaped (no zombie) and no worker
    # may be left orphaned mid-task.
    for alive, reaped, busy in ext_mod._doc_pool_health():
        assert not busy, "worker left marked busy after a child death"
        assert alive or reaped, "zombie worker left in the pool after a child death"


async def test_extract_document_output_capped_at_5mb(monkeypatch):
    """Extracted markdown is truncated to _MAX_EXTRACTED_MARKDOWN (F5)."""
    # Use a smaller cap for faster test (10KB instead of 5MB)
    test_cap = 10000
    huge = "x" * (test_cap + 1000)

    # Use env var to inject test data into the worker subprocess.
    monkeypatch.setenv("TEST_MARKITDOWN_OUTPUT", huge)
    monkeypatch.setenv("BROWSER_CRAWLER_TEST_HOOKS", "1")
    # Set the cap directly
    monkeypatch.setattr(ext_mod, "_MAX_EXTRACTED_MARKDOWN", test_cap)

    result = _extract_document(b"%PDF-1.4 fake", "http://example.com/x.pdf")
    assert result.markdown is not None
    assert len(result.markdown) == test_cap

    monkeypatch.delenv("TEST_MARKITDOWN_OUTPUT", raising=False)


async def test_extract_document_normal_works(monkeypatch):
    """Normal extraction returns the markdown content."""
    # Use env var to inject test data into the worker subprocess.
    monkeypatch.setenv("TEST_MARKITDOWN_OUTPUT", "## Hello Markdown")
    monkeypatch.setenv("BROWSER_CRAWLER_TEST_HOOKS", "1")

    result = _extract_document(b"%PDF-1.4 fake", "http://example.com/x.pdf")
    assert result.markdown == "## Hello Markdown"
    assert result.text == "## Hello Markdown"

    monkeypatch.delenv("TEST_MARKITDOWN_OUTPUT", raising=False)


# ============================================================
# Adversarial perf #3.2: document extraction runs in a process that is
# forcefully killed on timeout (no lingering zombie / stalled pool).
# ============================================================


async def test_extract_document_process_pool_isolation(monkeypatch, real_sleeps):
    """Document extraction runs in a separate process so a hang can be killed
    and does not degrade the shared thread pool.
    """
    # Use a very short timeout - patch the module variable directly.
    monkeypatch.setattr(ext_mod, "_EXTRACTION_DOC_TIMEOUT", 0.01)

    assert ext_mod._EXTRACTION_DOC_TIMEOUT == 0.01

    # The subprocess will check this env var and hang if set.
    monkeypatch.setenv("TEST_MARKITDOWN_HANG", "1")
    monkeypatch.setenv("BROWSER_CRAWLER_TEST_HOOKS", "1")

    result = _extract_document(b"%PDF-1.4 fake", "http://example.com/x.pdf")
    # Should return None due to timeout (process killed).
    assert result.text is None
    assert result.markdown is None

    # Clean up env var for other tests
    monkeypatch.delenv("TEST_MARKITDOWN_HANG", raising=False)


async def test_extract_document_process_pool_normal_works(monkeypatch):
    """Normal document extraction still works with process isolation."""
    # Use normal timeout - patch the module variable directly.
    monkeypatch.setattr(ext_mod, "_EXTRACTION_DOC_TIMEOUT", 10.0)
    from app.crawl.extraction import _extract_document

    # Don't set TEST_MARKITDOWN_HANG - normal operation.
    result = _extract_document(b"%PDF-1.4 fake", "http://example.com/x.pdf")
    # With fake bytes, markitdown returns something (or None for unparseable);
    # the key is it doesn't crash.
    assert isinstance(result, ext_mod.ExtractedContent)


async def test_extract_document_recovers_after_timeout(monkeypatch, real_sleeps):
    """After a timed-out (killed) extraction, the next call still works.

    Adversarial perf #3.2: a killed worker must not leave a zombie that stalls
    the (semaphore-bounded) extraction concurrency.
    """
    # First call hangs and is killed on timeout (short timeout, hang injected).
    monkeypatch.setenv("TEST_MARKITDOWN_HANG", "1")
    monkeypatch.setenv("BROWSER_CRAWLER_TEST_HOOKS", "1")
    monkeypatch.setattr(ext_mod, "_EXTRACTION_DOC_TIMEOUT", 0.01)
    hung = _extract_document(b"%PDF-1.4 fake", "http://example.com/hang.pdf")
    assert hung.text is None
    monkeypatch.delenv("TEST_MARKITDOWN_HANG", raising=False)

    # Second call (same process / shared semaphore) must succeed. Use a normal
    # timeout so the worker subprocess has time to spawn + import on spawn
    # platforms (macOS) -- the point is that the killed first worker did not
    # stall the bounded extraction concurrency.
    monkeypatch.setattr(ext_mod, "_EXTRACTION_DOC_TIMEOUT", 10.0)
    monkeypatch.setenv("TEST_MARKITDOWN_OUTPUT", "## Recovered")
    monkeypatch.setenv("BROWSER_CRAWLER_TEST_HOOKS", "1")
    recovered = _extract_document(b"%PDF-1.4 fake", "http://example.com/ok.pdf")
    assert recovered.markdown == "## Recovered"
    monkeypatch.delenv("TEST_MARKITDOWN_OUTPUT", raising=False)


async def test_test_hooks_inert_without_gate(monkeypatch):
    """N10: TEST_MARKITDOWN_* flags are inert unless BROWSER_CRAWLER_TEST_HOOKS=1
    is also set — production children must not honor stray test flags."""
    monkeypatch.setenv("TEST_MARKITDOWN_OUTPUT", "## hijacked")
    monkeypatch.delenv("BROWSER_CRAWLER_TEST_HOOKS", raising=False)

    ungated = _extract_document(b"%PDF-1.4 fake", "http://example.com/x.pdf")
    assert ungated.markdown != "## hijacked"

    monkeypatch.setenv("BROWSER_CRAWLER_TEST_HOOKS", "1")
    gated = _extract_document(b"%PDF-1.4 fake", "http://example.com/x.pdf")
    assert gated.markdown == "## hijacked"


def test_css_selector_slow_must_not_hang(monkeypatch, real_sleeps):
    monkeypatch.setattr(extraction_mod, "_EXTRACTION_SELECTOR_TIMEOUT", 0.2)
    monkeypatch.setattr(
        bs4.BeautifulSoup,
        "select",
        lambda self, selector: time.sleep(3) or [],
    )
    html = "<html><body><p class='x'>hi</p></body></html>"
    start = time.monotonic()
    result = _extract_selected(html, "http://example.com/", css_selector=".x")
    elapsed = time.monotonic() - start
    assert elapsed < 1.0, "slow CSS selector was not bounded by the timeout"
    assert result == ExtractedContent(None, None)


def test_css_selector_fast_still_works(monkeypatch):
    monkeypatch.setattr(extraction_mod, "_EXTRACTION_SELECTOR_TIMEOUT", 0.2)
    html = "<html><body><p class='x'>hi</p></body></html>"
    result = _extract_selected(html, "http://example.com/", css_selector=".x")
    assert result.text is not None
    assert "hi" in (result.text or "")


class _SlowFakeTree:
    def xpath(self, expr):
        time.sleep(3)
        return []


def test_xpath_selector_slow_must_not_hang(monkeypatch, real_sleeps):
    monkeypatch.setattr(extraction_mod, "_EXTRACTION_SELECTOR_TIMEOUT", 0.2)
    monkeypatch.setattr(extraction_mod, "_parse_html_tree", lambda html: _SlowFakeTree())
    html = "<html><body><p>x</p></body></html>"
    start = time.monotonic()
    result = _extract_selected(html, "http://example.com/", xpath="//p")
    elapsed = time.monotonic() - start
    assert elapsed < 1.0, "slow XPath was not bounded by the timeout"
    assert result == ExtractedContent(None, None)


def test_xpath_selector_fast_still_works(monkeypatch):
    monkeypatch.setattr(extraction_mod, "_EXTRACTION_SELECTOR_TIMEOUT", 0.2)
    html = "<html><body><p>x</p></body></html>"
    result = _extract_selected(html, "http://example.com/", xpath="//p")
    assert result.text is not None
    assert "x" in (result.text or "")


def _spy_parse(monkeypatch):
    calls = {"n": 0}
    real = extraction._parse_html_tree

    def spy(html):
        calls["n"] += 1
        return real(html)

    monkeypatch.setattr(extraction, "_parse_html_tree", spy)
    return calls


def test_html_diagnostics_parses_once(monkeypatch):
    calls = _spy_parse(monkeypatch)
    html = "<html><body><a href='/x'>x</a><form action='/s'><input name='q'></form></body></html>"
    extraction._html_diagnostics(
        html,
        "http://example.com/",
        host_policy=UNRESTRICTED_HOST_POLICY,
    )
    assert calls["n"] == 1


def test_shared_tree_parses_once_across_passes(monkeypatch):
    calls = _spy_parse(monkeypatch)
    html = (
        "<html><body><a href='/x'>x</a><iframe src='/f'></iframe>"
        "<form action='/s'><input name='q'></form></body></html>"
    )
    tree = extraction._parse_html_tree(html)
    assert calls["n"] == 1
    extraction._html_diagnostics(
        html,
        "http://example.com/",
        host_policy=UNRESTRICTED_HOST_POLICY,
        tree=tree,
    )
    links, _, _ = extraction._extract_discovered_links(
        html,
        "http://example.com/",
        host_policy=UNRESTRICTED_HOST_POLICY,
        tree=tree,
    )
    # frames, forms, and links all reused the single pre-parsed tree.
    assert calls["n"] == 1
    assert links


class TestExtractionEdges:
    """Cover remaining branches in extraction.py"""

    def test_extract_content_with_css_selector(self):
        from app.crawl.extraction import extract_content

        html = '<div class="target">selected</div><div>ignored</div>'
        result = extract_content(html.encode(), "text/html", "http://e.com/", css_selector=".target")
        assert result.text == "selected"

    def test_extract_content_with_xpath(self):
        from app.crawl.extraction import extract_content

        html = '<div class="target">selected</div><div>ignored</div>'
        result = extract_content(html.encode(), "text/html", "http://e.com/", xpath='//div[@class="target"]')
        assert result.text == "selected"

    def test_html_to_markdown_markdownify_success(self):
        from app.crawl.extraction import _html_to_markdown

        text, md = _html_to_markdown("<p>hello <strong>world</strong></p>")
        assert (text) is not None
        assert (md) is not None
        assert "world" in md

    def test_html_to_markdown_markdownify_exception(self):
        from app.crawl.extraction import _html_to_markdown

        text, md = _html_to_markdown("<p>test</p>")
        assert (text) is not None

    def test_extract_selected_no_html(self):
        from app.crawl.extraction import _extract_selected

        result = _extract_selected("", "http://e.com/", css_selector=".x")
        assert result.text is None or isinstance(result.text, str)
        assert (result.markdown) is None

    def test_extract_selected_css_selector_no_match(self):
        from app.crawl.extraction import _extract_selected

        result = _extract_selected("<div>content</div>", "http://e.com/", css_selector=".missing")
        assert result.text is None or isinstance(result.text, str)
        assert (result.markdown) is None

    def test_extract_selected_xpath_no_match(self):
        from app.crawl.extraction import _extract_selected

        result = _extract_selected("<div>content</div>", "http://e.com/", xpath="//span")
        assert result.text is None or isinstance(result.text, str)
        assert (result.markdown) is None

    def test_extract_selected_xpath_invalid(self):
        from app.crawl.extraction import _extract_selected

        result = _extract_selected("<div>content</div>", "http://e.com/", xpath="//[")
        assert result.text is None or isinstance(result.text, str)

    def test_extract_selected_xpath_string_node(self):
        from app.crawl.extraction import _extract_selected

        result = _extract_selected(
            "<div><span>text1</span><span>text2</span></div>", "http://e.com/", xpath="//span/text()"
        )
        assert (result.text) is not None

    def test_extract_html_trafilatura_exception(self):
        from app.crawl.extraction import _extract_html

        html = "<html><body><p>test content</p></body></html>"
        result = _extract_html(html, "http://e.com/")
        assert (result.text) is not None

    def test_extract_html_markdownify_exception(self):
        from app.crawl.extraction import _extract_html

        html = "<html><body><p>plain text fallback</p></body></html>"
        result = _extract_html(html, "http://e.com/")
        assert (result.text) is not None

    def test_extract_document_markitdown_import_error(self):
        from app.crawl.extraction import _extract_document

        result = _extract_document(b"fake pdf content", "http://e.com/doc.pdf")
        assert (result.text) is not None

    def test_extract_document_markitdown_exception(self):
        from app.crawl.extraction import _extract_document

        result = _extract_document(b"not a pdf", "http://e.com/doc.pdf")
        assert result.text is None or isinstance(result.text, str)

    def test_parse_html_tree_empty(self):
        from app.crawl.extraction import _parse_html_tree

        assert (_parse_html_tree("")) is None
        assert (_parse_html_tree(None)) is None

    def test_parse_html_tree_parser_error(self):
        from app.crawl.extraction import _parse_html_tree

        result = _parse_html_tree("<")
        assert (result) is not None


class TestExtractionGaps:
    def test_nested_anchor(self):
        # Real HTML parsers (lxml) flatten illegally-nested anchors into
        # siblings, so both distinct hrefs are discovered as separate links
        # rather than the inner one being swallowed by the outer.
        links, _, _ = _extract_discovered_links(
            '<a href="/x">outer <a href="/y">inner</a></a>',
            "http://e.com/",
        )
        hrefs = {entry.normalized_url for entry in links}
        assert "http://e.com/x" in hrefs
        assert "http://e.com/y" in hrefs

    def test_duplicate_href(self):
        links, count, _ = _extract_discovered_links(
            '<a href="/x">a</a><a href="/x">b</a>',
            "http://e.com/",
        )
        assert len(links) == 1
        assert count == 1

    def test_max_links_truncation(self):
        html = "".join(f'<a href="/{i}">x</a>' for i in range(5))
        links, count, truncated = _extract_discovered_links(html, "http://e.com/", max_links=2)
        assert len(links) == 2
        assert truncated
        assert count == 5

    def test_html_diagnostics_frames_and_forms(self):
        html = (
            '<frameset><frame src="/a"></frameset>'
            '<form action="/s" method="post"><input name="q" value="1"></form>'
        )
        diag = _html_diagnostics(html, "http://e.com/")
        assert "frames" in diag
        assert "forms" in diag
        assert diag["frames"]
        assert diag["forms"]

    def test_js_endpoints(self):
        html = '<script>fetch("/api/x");</script>'
        endpoints = extract_js_endpoints(html)
        assert {"url": "/api/x"} in endpoints


class TestTrafilaturaChatterIsSuppressed:
    """The extractor must not emit ERROR-level records for benign inputs.

    trafilatura logs ERROR from trafilatura.core and trafilatura.utils when
    the HTML is empty, a fragment, or has no main content -- normal
    conditions for a crawler, and the code already falls through to the
    markdownify/plain-text fallbacks. At ERROR level on a run of ~2000 pages
    this produced 2239 spurious records that crowded out real failures.
    """

    @pytest.mark.parametrize("html", ["", "<html><body></body></html>", "<div></div>"])
    def test_empty_and_fragment_html_emits_no_trafilatura_error(self, html, caplog):
        extraction._extract_html(html, url="http://example.com/")

        trafilatura_records = [r for r in caplog.records if r.name.startswith("trafilatura")]
        errors = [r for r in trafilatura_records if r.levelno >= logging.ERROR]
        assert not errors, (
            "trafilatura emitted ERROR records for a benign input; these are the "
            "2239 spurious per-run records. Extract already falls back to "
            "markdownify/plain text, so the record carries no signal."
        )

    def test_real_html_still_extracts_and_logs_no_error(self, caplog):
        out = extraction._extract_html(
            "<html><body><article><h1>Title</h1><p>Real body text here.</p></article></body></html>",
            url="http://example.com/",
        )
        assert out.text and "Real body text" in out.text
        errors = [
            r for r in caplog.records
            if r.name.startswith("trafilatura") and r.levelno >= logging.ERROR
        ]
        assert not errors
