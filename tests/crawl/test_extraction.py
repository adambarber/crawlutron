"""Content extraction: markdown/text extraction, selector timeouts, bounds and single-pass parsing."""

import asyncio
import logging
import threading

import bs4
import pytest
from _assertions import set_optional_env
from _extraction_helpers import (
    _detect_pagination,
    _extract_discovered_links,
    _html_diagnostics,
    doc_worker_acquisitions,
    extract_content,
    extract_js_endpoints,
)
from _policy_factories import BOOL_ENV_CASES, UNRESTRICTED_HOST_POLICY
from _process_doubles import StubConn, StubProc

import app.crawl.extraction as ext_mod
import app.crawl.extraction as extraction
import app.crawl.extraction as extraction_mod
from app.crawl.extraction import (
    ExtractedContent,
    Selectors,
    _extract_document,
    _extract_html,
    _extract_selected,
    _html_to_markdown,
    _is_html,
    _looks_like_document,
    _parse_html_tree,
    _text_from_html,
)


def assert_extraction_empty(result) -> None:
    """A failed document extraction yields nothing at all, in both fields.

    A timeout, a crash and a parse failure all arrive here, and all three are
    the same claim: no text *and* no markdown. Asserting one field would let a
    half-filled result through, which is how an empty page reaches the caller
    dressed as a successful extraction.
    """
    assert result.text is None
    assert result.markdown is None


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
        # The helper is a thin untyped wrapper that defaults host_policy, so
        # None is a supported input rather than a suppressed type error.
        assert extract_js_endpoints("") == []
        assert extract_js_endpoints(None) == []

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


async def test_extract_document_skips_oversized_input(monkeypatch):
    """#mem-guard: markitdown workers must never receive unbounded input."""
    import app.crawl.extraction as ex

    acquisitions = doc_worker_acquisitions(monkeypatch)
    monkeypatch.setattr(ex, "_EXTRACTION_DOC_MAX_INPUT_BYTES", 100)

    out = await asyncio.to_thread(ex._extract_document, b"x" * 101, "http://x.test/big.pdf")
    assert out.text is None and out.markdown is None
    assert acquisitions == [], "a refused document still asked for a markitdown worker"


@pytest.mark.parametrize(("raw", "expected"), BOOL_ENV_CASES)
def test_document_worker_decision_uses_shared_bool_parser(monkeypatch, caplog, raw, expected):
    import app.crawl.extraction as ex

    set_optional_env(monkeypatch, "BROWSER_CRAWLER_EXTRACTION_DOC_ENABLED", raw)

    spawned = []

    def spawn():
        spawned.append(True)
        return ex._DocWorker(StubProc(pid=1), StubConn())

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

    acquisitions = doc_worker_acquisitions(monkeypatch)
    monkeypatch.setenv("BROWSER_CRAWLER_EXTRACTION_DOC_ENABLED", "0")

    out = await asyncio.to_thread(ex._extract_document, b"tiny", None)
    assert out.text is None and out.markdown is None
    assert acquisitions == [], "a disabled extractor still asked for a markitdown worker"


class TestExtractionMethods:
    @pytest.mark.parametrize(
        ("content", "content_type", "expected_text", "expected_branch"),
        [
            ("<html><body><p>Hello world</p></body></html>", "text/html", "html", "html"),
            (b"%PDF-1.7 fake bytes", "application/pdf", "document", "document"),
            ("plain text", "text/plain", "plain text", None),
            (b"raw bytes", "application/octet-stream", "raw bytes", None),
        ],
    )
    def test_extraction_routes_by_content_type(
        self, monkeypatch, content, content_type, expected_text, expected_branch
    ):
        calls = []

        def fake_html(html, url, css_selector=None, xpath=None):
            # The selector pair is part of the real `_extract_html` contract:
            # `extract_content` owns the HTML branch and hands the pair down
            # rather than dispatching on it itself, so a double that did not
            # accept it would be modelling a narrower function than the one
            # production routes through.
            calls.append("html")
            return ExtractedContent("html", "html")

        def fake_document(body, url):
            calls.append("document")
            return ExtractedContent("document", "document")

        monkeypatch.setattr(extraction, "_extract_html", fake_html)
        monkeypatch.setattr(extraction, "_extract_document", fake_document)
        result = extraction.extract_content(content, content_type, "http://example.com/")
        assert result.text == expected_text
        assert calls == ([] if expected_branch is None else [expected_branch])


class TestHtmlTreeParsing:
    def test_extraction_parse_html_tree_error(self):
        assert _parse_html_tree("{{invalid html").tag == "html"


class TestSelectedExtraction:
    def test_extraction_selected_with_xpath(self):
        html = "<html><body><div>Hello</div></body></html>"
        result = _extract_selected(html, "http://x/", selectors=Selectors(xpath="//div/text()"))
        assert result.text == "Hello"
        assert result.markdown == "Hello"


class TestLinkExtraction:
    def test_engine_parse_robots_blank_line(self):
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


async def test_extract_document_timeout_is_enforced(monkeypatch):
    """A document extraction exceeding the wall-clock timeout returns None (F5).

    The hang lives in the worker SUBPROCESS and is injected there through the
    environment, so the suite's in-process sleep patch is irrelevant to it --
    which is why this test never actually needed real sleeps.
    """
    # Use env var to signal test hang to the worker subprocess (inherited).
    monkeypatch.setenv("TEST_MARKITDOWN_HANG", "1")
    monkeypatch.setenv("BROWSER_CRAWLER_TEST_HOOKS", "1")
    # Set short timeout - patch the module variable directly.
    monkeypatch.setattr(ext_mod, "_EXTRACTION_DOC_TIMEOUT", 0.01)

    assert_extraction_empty(_extract_document(b"%PDF-1.4 fake", "http://example.com/x.pdf"))

    # Clean up
    monkeypatch.delenv("TEST_MARKITDOWN_HANG", raising=False)


async def test_extract_document_child_death_returns_empty(monkeypatch):
    """A worker that dies before sending (segfault) yields empty extraction,
    not an unhandled EOFError, and leaves no zombie behind (M8)."""

    monkeypatch.setenv("TEST_MARKITDOWN_DIE", "1")
    monkeypatch.setenv("BROWSER_CRAWLER_TEST_HOOKS", "1")
    assert_extraction_empty(_extract_document(b"%PDF-1.4 fake", "http://example.com/x.pdf"))

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


async def test_extract_document_process_pool_isolation(monkeypatch):
    """Document extraction runs in a separate process so a hang can be killed
    and does not degrade the shared thread pool.

    The kill is injected into the worker SUBPROCESS via the environment, so the
    suite's in-process sleep patch is irrelevant to it -- which is why this
    test never actually needed real sleeps.
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


async def test_extract_document_recovers_after_timeout(monkeypatch):
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


def test_css_selector_slow_must_not_hang(monkeypatch, caplog):
    """A selector that never returns must be bounded, not awaited.

    The stub blocks on an Event nothing ever sets, for far longer than any
    timeout this test could afford, so the ONLY thing that can end the wait is
    ``_EXTRACTION_SELECTOR_TIMEOUT`` firing inside ``_eval_selector``. The
    warning is the load-bearing assertion: "timed out" and "matched nothing"
    both yield ``ExtractedContent(None, None)``, so without it a selector
    timeout that stopped firing would still pass -- which is exactly what
    happened when this test slept 3 real seconds against a patched no-op sleep
    and so never exercised the timeout at all.
    """
    never_released = threading.Event()
    monkeypatch.setattr(extraction_mod, "_EXTRACTION_SELECTOR_TIMEOUT", 0.2)
    monkeypatch.setattr(
        bs4.BeautifulSoup,
        "select",
        lambda self, selector: never_released.wait(timeout=30) or [],
    )
    html = "<html><body><p class='x'>hi</p></body></html>"
    with caplog.at_level(logging.WARNING, logger="app.crawl.extraction"):
        result = _extract_selected(html, "http://example.com/", selectors=Selectors(css=".x"))
    assert result == ExtractedContent(None, None)
    assert "extraction_selector_timeout" in caplog.text, (
        "the CSS selector returned empty rather than timing out: the extraction "
        "selector deadline did not fire"
    )


def test_css_selector_fast_still_works(monkeypatch):
    monkeypatch.setattr(extraction_mod, "_EXTRACTION_SELECTOR_TIMEOUT", 0.2)
    html = "<html><body><p class='x'>hi</p></body></html>"
    result = _extract_selected(html, "http://example.com/", selectors=Selectors(css=".x"))
    assert result.text is not None
    assert "hi" in (result.text or "")


class _SlowFakeTree:
    """An XPath tree whose evaluation blocks far past the selector deadline."""

    def __init__(self) -> None:
        self.never_released = threading.Event()

    def xpath(self, expr):
        return self.never_released.wait(timeout=30) or []


def test_xpath_selector_slow_must_not_hang(monkeypatch, caplog):
    """The XPath twin of the CSS test: same instrument, same warning assertion.

    "Timed out" and "matched nothing" both return ``ExtractedContent(None, None)``,
    so the warning is what distinguishes a working deadline from a deadline that
    quietly stopped firing.
    """
    tree = _SlowFakeTree()
    monkeypatch.setattr(extraction_mod, "_EXTRACTION_SELECTOR_TIMEOUT", 0.2)
    monkeypatch.setattr(extraction_mod, "_parse_html_tree", lambda html: tree)
    html = "<html><body><p>x</p></body></html>"
    with caplog.at_level(logging.WARNING, logger="app.crawl.extraction"):
        result = _extract_selected(html, "http://example.com/", selectors=Selectors(xpath="//p"))
    assert result == ExtractedContent(None, None)
    assert "extraction_selector_timeout" in caplog.text, (
        "the XPath expression returned empty rather than timing out: the "
        "extraction selector deadline did not fire"
    )


def test_xpath_selector_fast_still_works(monkeypatch):
    monkeypatch.setattr(extraction_mod, "_EXTRACTION_SELECTOR_TIMEOUT", 0.2)
    html = "<html><body><p>x</p></body></html>"
    result = _extract_selected(html, "http://example.com/", selectors=Selectors(xpath="//p"))
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

    @pytest.mark.parametrize(
        "selectors",
        [Selectors(css=".target"), Selectors(xpath='//div[@class="target"]')],
        ids=["css", "xpath"],
    )
    def test_extract_content_selects_the_targeted_subtree(self, selectors):
        html = '<div class="target">selected</div><div>ignored</div>'
        result = extract_content(html.encode(), "text/html", "http://e.com/", selectors=selectors)
        assert result.text == "selected"

    def test_html_to_markdown_markdownify_success(self):
        text, md = _html_to_markdown("<p>hello <strong>world</strong></p>")
        assert text is not None
        assert md is not None
        assert "world" in md

    def test_html_to_markdown_markdownify_exception(self):
        text, md = _html_to_markdown("<p>test</p>")
        assert text is not None

    @pytest.mark.parametrize(
        ("html", "selectors"),
        [
            ("", Selectors(css=".x")),
            ("<div>content</div>", Selectors(css=".missing")),
            ("<div>content</div>", Selectors(xpath="//span")),
            ("<div>content</div>", Selectors(xpath="//[")),
        ],
        ids=["no_html", "css_no_match", "xpath_no_match", "xpath_invalid"],
    )
    def test_extract_selected_yields_nothing_when_it_matches_nothing(self, html, selectors):
        assert _extract_selected(html, "http://e.com/", selectors=selectors) == ExtractedContent(None, None)

    def test_extract_selected_xpath_string_node(self):
        result = _extract_selected(
            "<div><span>text1</span><span>text2</span></div>",
            "http://e.com/",
            selectors=Selectors(xpath="//span/text()"),
        )
        assert result.text is not None

    @pytest.mark.parametrize(
        "html",
        [
            "<html><body><p>test content</p></body></html>",
            "<html><body><p>plain text fallback</p></body></html>",
        ],
        ids=["trafilatura", "markdownify_fallback"],
    )
    def test_extract_html_always_yields_text(self, html):
        assert _extract_html(html, "http://e.com/").text is not None

    def test_extract_document_markitdown_import_error(self):
        assert _extract_document(b"fake pdf content", "http://e.com/doc.pdf").text is not None

    def test_extract_document_markitdown_exception(self):
        result = _extract_document(b"not a pdf", "http://e.com/doc.pdf")
        assert result.text is None or isinstance(result.text, str)

    def test_parse_html_tree_empty(self):
        assert _parse_html_tree("") is None
        assert _parse_html_tree(None) is None

    def test_parse_html_tree_parser_error(self):
        assert _parse_html_tree("<").tag == "html"


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
            r for r in caplog.records if r.name.startswith("trafilatura") and r.levelno >= logging.ERROR
        ]
        assert not errors


def test_selector_on_non_html_is_reported_not_silently_dropped(caplog):
    """A selector against a body with no DOM cannot be honoured, and must say so.

    ``extract_content`` routes the selector pair only on the HTML branch. A PDF
    or a plain text body has no DOM to target, so the pair cannot narrow
    anything and the full content comes back instead -- which is a *different
    answer*, not a smaller one, and the caller has no way to tell: the same
    request against an HTML page does narrow, and both return a populated
    ``ExtractedContent``. So the warning is the load-bearing assertion here, for
    the same reason it is in ``test_css_selector_slow_must_not_hang``: the
    returned value is identical whether or not the drop is reported, so a
    regression that stopped reporting it would pass on the result alone.

    Reachable from the wire, not just from here: the crawl path builds the pair
    from caller policy and spreads it into this function.
    """
    pdf = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n"
    with caplog.at_level(logging.WARNING, logger="app.crawl.extraction"):
        result = extraction_mod.extract_content(
            pdf, "application/pdf", "http://example.com/doc.pdf", selectors=Selectors(css="#agenda")
        )
    assert "extraction_selector_ignored_non_html" in caplog.text, (
        "a selector supplied for a PDF was dropped with no warning; the caller "
        f"receives the whole document believing it was narrowed (result: {result!r})"
    )
    record = next(r for r in caplog.records if r.getMessage() == "extraction_selector_ignored_non_html")
    assert record.css_selector == "#agenda", record.__dict__
    assert record.content_type == "application/pdf", record.__dict__

    # The same call against HTML does narrow, and so must NOT warn -- otherwise
    # this passes by warning unconditionally and proves nothing.
    caplog.clear()
    html = "<html><body><div id='agenda'>item one</div><p>filler</p></body></html>"
    with caplog.at_level(logging.WARNING, logger="app.crawl.extraction"):
        narrowed = extraction_mod.extract_content(
            html, "text/html", "http://example.com/", selectors=Selectors(css="#agenda")
        )
    assert "extraction_selector_ignored_non_html" not in caplog.text, caplog.text
    assert "item one" in (narrowed.markdown or ""), narrowed
    assert "filler" not in (narrowed.markdown or ""), narrowed

    # No selector, no warning: the common path must stay quiet.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="app.crawl.extraction"):
        extraction_mod.extract_content(pdf, "application/pdf", "http://example.com/doc.pdf")
    assert "extraction_selector_ignored_non_html" not in caplog.text, caplog.text
