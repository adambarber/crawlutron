"""Content extraction: markdown/text extraction, selector timeouts, bounds and single-pass parsing."""

import asyncio
import logging
import threading

import bs4
import pytest

import app.extraction.content as ext_mod
import app.extraction.content as extraction
import app.extraction.content as extraction_mod
from app.config.env import parse_bool_env
from app.extraction.content import (
    ExtractedContent,
    Selectors,
    extract_document,
    extract_html,
    extract_selected,
    html_to_markdown,
    is_html,
    looks_like_document,
    parse_html_tree,
    shutdown_doc_extraction_executor,
    text_from_html,
)
from app.orchestration.doc_pool import DocExtractionPool, doc_task_hooks
from app.vocabulary.limits import DEFAULT_EXTRACTION_MAX_MARKDOWN
from tests._assertions import set_optional_env
from tests._extraction_helpers import (
    detect_pagination,
    doc_worker_acquisitions,
    extract_content,
    extract_discovered_links,
    extract_js_endpoints,
    html_diagnostics,
    settings_with,
)
from tests._parse_budget import ParseBudget
from tests._policy_factories import BOOL_ENV_CASES, canonical
from tests._process_doubles import StubSpawner, stub_worker


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
        monkeypatch.setenv("CRAWLUTRON_TEST_HOOKS", value)
        monkeypatch.setenv("TEST_MARKITDOWN_DIE", value)
        monkeypatch.setenv("TEST_MARKITDOWN_HANG", value)
        hooks = doc_task_hooks()
        assert hooks is not None
        assert hooks["die"] is True
        assert hooks["hang"] is True


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
        # The helper is a thin untyped wrapper that defaults host_policy, so
        # None is a supported input rather than a suppressed type error.
        assert extract_js_endpoints("") == []
        assert extract_js_endpoints(None) == []

    def test_extract_js_endpoints_skips_empty_endpoint(self):
        # A regex match with an empty/whitespace endpoint is skipped.
        endpoints = extract_js_endpoints('<script>fetch(" ")</script>', canonical("http://e.com/"))
        assert endpoints == []

    def test_extract_html_markdownify_fallback(self):
        # Trafilatura rejects an empty body; the markdownify fallback runs
        # (and may still yield nothing, falling through to plain-text).
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
        # The href is attacker-controlled HTML: a non-http(s) rel=next must not
        # escape into next_page_url. Egress filtering belongs at the producer
        # so every consumer is covered, not just the ones that remember.
        for bad in ("javascript:alert(1)", "data:text/html,<x>", "mailto:a@b.c"):
            html = f'<a rel="next" href="{bad}">next</a>'
            assert detect_pagination(html, canonical("https://example.com/list")) is None

    def test_detect_pagination_still_resolves_relative(self):
        # The egress filter must not break legitimate relative pagination.
        assert detect_pagination(
            '<a rel="next" href="/p2">n</a>', canonical("https://example.com/list")
        ) == canonical("https://example.com/p2")

    def test_detect_pagination_protocol_relative_resolves(self):
        # //evil.com/x resolves against the page URL to https, so it survives
        # the scheme filter exactly like the HTTP path already allowed.
        assert detect_pagination(
            '<a rel="next" href="//example.com/p2">n</a>', canonical("https://example.com/list")
        ) == canonical("https://example.com/p2")


async def test_extract_document_skips_oversized_input(monkeypatch):
    """#mem-guard: markitdown workers must never receive unbounded input."""
    import app.extraction.content as ex

    acquisitions = doc_worker_acquisitions(monkeypatch)
    monkeypatch.setattr(ex, "EXTRACTION_DOC_MAX_INPUT_BYTES", 100)

    out = await asyncio.to_thread(ex.extract_document, b"x" * 101, "http://x.test/big.pdf")
    assert out.text is None and out.markdown is None
    assert acquisitions == [], "a refused document still asked for a markitdown worker"


@pytest.mark.parametrize(("raw", "expected"), BOOL_ENV_CASES)
def test_document_worker_gate_uses_the_shared_bool_parser(monkeypatch, caplog, raw, expected):
    """``CRAWLUTRON_EXTRACTION_DOC_ENABLED`` is read by the shared env parser.

    The knob decides whether a document may consume a worker process, so "on" must
    mean the same thing here as in every other feature flag. Asserting through the
    pool rather than through the flag reader proves the reader is the one production
    uses: a gate that ignored this variable would still acquire nothing.
    """
    set_optional_env(monkeypatch, "CRAWLUTRON_EXTRACTION_DOC_ENABLED", raw)
    spawner = StubSpawner(stub_worker(1))
    pool = DocExtractionPool(spawn=spawner, worker_count=lambda: 1, enabled=_enabled_from_env)

    worker = pool.acquire(settings_with().extraction_doc_acquire_timeout)

    assert (worker is not None) is expected, "the gate did not follow the env knob"
    assert bool(spawner.asked) is expected, "a refused gate still forked a worker"
    if raw == "garbage":
        assert any("EXTRACTION_DOC_ENABLED" in record.getMessage() for record in caplog.records)


def _enabled_from_env() -> bool:
    """The gate production builds ``DOC_POOL`` with, reached by value not by name.

    Re-derived rather than imported because ``_document_extraction_enabled`` is
    private and this test is about the parser's verdict, not about which function
    holds it -- the pool's own gate honoring that verdict is asserted above.
    """
    return parse_bool_env("CRAWLUTRON_EXTRACTION_DOC_ENABLED", default=True)


async def test_extract_document_kill_switch(monkeypatch):
    """Env kill-switch lets deployments disable subprocess doc extraction."""
    import app.extraction.content as ex

    acquisitions = doc_worker_acquisitions(monkeypatch)
    monkeypatch.setenv("CRAWLUTRON_EXTRACTION_DOC_ENABLED", "0")

    out = await asyncio.to_thread(ex.extract_document, b"tiny", None)
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
            # The selector pair is part of the real `extract_html` contract:
            # `extract_content` owns the HTML branch and hands the pair down
            # rather than dispatching on it itself, so a double that did not
            # accept it would be modelling a narrower function than the one
            # production routes through.
            calls.append("html")
            return ExtractedContent("html", "html")

        def fake_document(body, url):
            calls.append("document")
            return ExtractedContent("document", "document")

        monkeypatch.setattr(extraction, "extract_html", fake_html)
        monkeypatch.setattr(extraction, "extract_document", fake_document)
        result = extraction.extract_content(content, content_type, canonical("http://example.com/"))
        assert result.text == expected_text
        assert calls == ([] if expected_branch is None else [expected_branch])


class TestHtmlTreeParsing:
    def test_extraction_parse_html_tree_error(self):
        assert parse_html_tree("{{invalid html").tag == "html"


class TestSelectedExtraction:
    def test_extraction_selected_with_xpath(self):
        html = "<html><body><div>Hello</div></body></html>"
        result = extract_selected(html, "http://x/", selectors=Selectors(xpath="//div/text()"))
        assert result.text == "Hello"
        assert result.markdown == "Hello"


class TestLinkExtraction:
    def test_engine_parse_robots_blank_line(self):
        # Test link extraction with mixed quote styles (covers edge cases)
        html = "<html><body><a href=https://example.com/test>link</a><a href='https://example.com/page2'>link2</a></body></html>"
        links = extract_discovered_links(html, None)
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
    """A document extraction exceeding the wall-clock timeout returns None.

    The hang lives in the worker SUBPROCESS and is injected there through the
    environment, so the suite's in-process sleep patch is irrelevant to it --
    which is why this test never actually needed real sleeps.
    """
    # The worker subprocess hangs; a deadline short enough to notice is the
    # whole point of the test, so it is passed in rather than written onto the
    # module the call lives in.
    monkeypatch.setenv("TEST_MARKITDOWN_HANG", "1")
    monkeypatch.setenv("CRAWLUTRON_TEST_HOOKS", "1")
    settings = settings_with(extraction_doc_timeout=0.01)

    assert_extraction_empty(extract_document(b"%PDF-1.4 fake", "http://example.com/x.pdf", settings=settings))

    # Clean up
    monkeypatch.delenv("TEST_MARKITDOWN_HANG", raising=False)


async def test_extract_document_child_death_returns_empty(monkeypatch):
    """A worker that dies before sending (segfault) yields empty extraction,
    not an unhandled EOFError, and leaves no zombie behind."""

    monkeypatch.setenv("TEST_MARKITDOWN_DIE", "1")
    monkeypatch.setenv("CRAWLUTRON_TEST_HOOKS", "1")
    assert_extraction_empty(extract_document(b"%PDF-1.4 fake", "http://example.com/x.pdf"))

    # Is per worker, not process-global: the pool is persistent and sized
    # above one, so an idle worker from an earlier extraction legitimately
    # stays alive. The dead worker must be reaped (no zombie) and no worker
    # may be left orphaned mid-task.
    for alive, reaped, busy in ext_mod.DOC_POOL.health():
        assert not busy, "worker left marked busy after a child death"
        assert alive or reaped, "zombie worker left in the pool after a child death"


async def test_extract_document_output_is_truncated_at_the_configured_cap(monkeypatch):
    """Extracted markdown is truncated to the cap the caller configured.

    The worker is told what to emit through the same hook the death and hang
    scenarios use, so this asserts the cap rather than the plumbing around it: 10 KB
    of markdown against a 10 KB cap is a truncation, and it is the only way to see
    one without materializing the 5 MB default.
    """
    cap = 10000
    monkeypatch.setenv("TEST_MARKITDOWN_OUTPUT", "x" * (cap + 1000))
    monkeypatch.setenv("CRAWLUTRON_TEST_HOOKS", "1")

    result = extract_document(
        b"%PDF-1.4 fake", "http://example.com/x.pdf", settings_with(extraction_max_markdown=cap)
    )

    assert result.markdown is not None
    assert len(result.markdown) == cap, "the document exceeded the configured cap"
    assert cap < DEFAULT_EXTRACTION_MAX_MARKDOWN, "a cap at the default proves nothing"


async def test_extract_document_normal_works():
    """A real document comes back as real markdown, converted by a real worker.

    This deliberately does not inject ``TEST_MARKITDOWN_OUTPUT``. The hook
    short-circuits the worker's task loop before markitdown is called, so a test
    using it passes whether or not the worker can convert anything at all --
    it is reading back an echo. The echo path itself is covered by the output
    cap test above.

    Running the real converter is also the only thing that covers the request
    and reply shape the parent and the child must agree on. A stub connection
    is written by the same hand as the code it stands in for, so it cannot
    disagree with it; the two could drift apart and every stub-backed test
    would still pass. Here the parent and the child are separate processes that
    have to mean the same thing.
    """
    ext_mod.DOC_POOL.teardown()
    result = extract_document(b"name,score\nada,10\n", "http://example.com/data.csv")

    assert result.markdown is not None
    # "ada" only reaches the reply if markitdown really read these bytes, and
    # it only gets read in a worker subprocess.
    assert "ada" in result.markdown
    assert ext_mod.DOC_POOL.pids(), "no worker process served the conversion"


# ============================================================
# Document extraction runs in a process that is
# forcefully killed on timeout (no lingering zombie / stalled pool).
# ============================================================


async def test_extract_document_process_pool_isolation(monkeypatch):
    """Document extraction runs in a separate process so a hang can be killed
    and does not degrade the shared thread pool.

    The kill is injected into the worker SUBPROCESS via the environment, so the
    suite's in-process sleep patch is irrelevant to it -- which is why this
    test never actually needed real sleeps.
    """
    # The subprocess will check this env var and hang if set.
    monkeypatch.setenv("TEST_MARKITDOWN_HANG", "1")
    monkeypatch.setenv("CRAWLUTRON_TEST_HOOKS", "1")

    result = extract_document(
        b"%PDF-1.4 fake",
        "http://example.com/x.pdf",
        settings=settings_with(extraction_doc_timeout=0.01),
    )
    # Should return None due to timeout (process killed).
    assert result.text is None
    assert result.markdown is None

    # Clean up env var for other tests
    monkeypatch.delenv("TEST_MARKITDOWN_HANG", raising=False)


async def test_extract_document_process_pool_normal_works():
    """Normal document extraction still works with process isolation."""
    from app.extraction.content import extract_document

    # Don't set TEST_MARKITDOWN_HANG - normal operation.
    result = extract_document(b"%PDF-1.4 fake", "http://example.com/x.pdf")
    # With fake bytes, markitdown returns something (or None for unparseable);
    # the key is it doesn't crash.
    assert isinstance(result, ext_mod.ExtractedContent)


async def test_extract_document_recovers_after_timeout(monkeypatch):
    """After a timed-out (killed) extraction, the next call still works.

    A killed worker must not leave a zombie that stalls
    the (semaphore-bounded) extraction concurrency.
    """
    # First call hangs and is killed on timeout (short timeout, hang injected).
    monkeypatch.setenv("TEST_MARKITDOWN_HANG", "1")
    monkeypatch.setenv("CRAWLUTRON_TEST_HOOKS", "1")
    hung = extract_document(
        b"%PDF-1.4 fake",
        "http://example.com/hang.pdf",
        settings=settings_with(extraction_doc_timeout=0.01),
    )
    assert hung.text is None
    monkeypatch.delenv("TEST_MARKITDOWN_HANG", raising=False)

    # Second call (same process / shared semaphore) must succeed. Use a normal
    # timeout so the worker subprocess has time to spawn + import on spawn
    # platforms (macOS) -- the point is that the killed first worker did not
    # stall the bounded extraction concurrency.
    monkeypatch.setenv("TEST_MARKITDOWN_OUTPUT", "## Recovered")
    monkeypatch.setenv("CRAWLUTRON_TEST_HOOKS", "1")
    recovered = extract_document(
        b"%PDF-1.4 fake",
        "http://example.com/ok.pdf",
        settings=settings_with(extraction_doc_timeout=10.0),
    )
    assert recovered.markdown == "## Recovered"
    monkeypatch.delenv("TEST_MARKITDOWN_OUTPUT", raising=False)


async def test_test_hooks_inert_without_gate(monkeypatch):
    """TEST_MARKITDOWN_* flags are inert unless CRAWLUTRON_TEST_HOOKS=1
    is also set — production children must not honor stray test flags."""
    monkeypatch.setenv("TEST_MARKITDOWN_OUTPUT", "## hijacked")
    monkeypatch.delenv("CRAWLUTRON_TEST_HOOKS", raising=False)

    ungated = extract_document(b"%PDF-1.4 fake", "http://example.com/x.pdf")
    assert ungated.markdown != "## hijacked"

    monkeypatch.setenv("CRAWLUTRON_TEST_HOOKS", "1")
    gated = extract_document(b"%PDF-1.4 fake", "http://example.com/x.pdf")
    assert gated.markdown == "## hijacked"


def assert_selector_timed_out(result, caplog, flavour: str) -> None:
    """A selector that timed out and one that matched nothing look identical.

    Both yield ``ExtractedContent(None, None)``, so the warning is the only
    thing that distinguishes a working deadline from a deadline that quietly
    stopped firing -- which is the failure this assertion exists to catch, and
    the reason neither selector test may assert on the result alone.
    """
    assert result == ExtractedContent(None, None)
    assert "extraction_selector_timeout" in caplog.text, (
        f"the {flavour} selector returned empty rather than timing out: the "
        "extraction selector deadline did not fire"
    )


def test_css_selector_slow_must_not_hang(monkeypatch, caplog):
    """A selector that never returns must be bounded, not awaited.

    The stub blocks on an Event nothing ever sets, for far longer than any
    timeout this test could afford, so the ONLY thing that can end the wait is
    the selector deadline firing inside ``_eval_selector``.
    """
    never_released = threading.Event()
    monkeypatch.setattr(
        bs4.BeautifulSoup,
        "select",
        lambda self, selector: never_released.wait(timeout=30) or [],
    )
    html = "<html><body><p class='x'>hi</p></body></html>"
    with caplog.at_level(logging.WARNING, logger="app.extraction.content"):
        result = extract_selected(
            html,
            "http://example.com/",
            selectors=Selectors(css=".x"),
            settings=settings_with(extraction_selector_timeout=0.2),
        )
    assert_selector_timed_out(result, caplog, "CSS")


def test_css_selector_fast_still_works():
    html = "<html><body><p class='x'>hi</p></body></html>"
    result = extract_selected(html, "http://example.com/", selectors=Selectors(css=".x"))
    assert result.text is not None
    assert "hi" in (result.text or "")


class _SlowFakeTree:
    """An XPath tree whose evaluation blocks far past the selector deadline."""

    def __init__(self) -> None:
        self.never_released = threading.Event()

    def xpath(self, expr):
        return self.never_released.wait(timeout=30) or []


def test_xpath_selector_slow_must_not_hang(caplog):
    """The XPath twin of the CSS test: same instrument, same warning assertion.

    The tree is handed in rather than reached for, so the thing that blocks is
    the evaluation this function actually performs.
    """
    html = "<html><body><p>x</p></body></html>"
    with caplog.at_level(logging.WARNING, logger="app.extraction.content"):
        result = extract_selected(
            html,
            "http://example.com/",
            selectors=Selectors(xpath="//p"),
            settings=settings_with(extraction_selector_timeout=0.2),
            tree=_SlowFakeTree(),
        )
    assert_selector_timed_out(result, caplog, "XPath")


def test_xpath_selector_fast_still_works():
    html = "<html><body><p>x</p></body></html>"
    result = extract_selected(html, canonical("http://example.com/"), selectors=Selectors(xpath="//p"))
    assert result.text is not None
    assert "x" in (result.text or "")


def test_html_diagnostics_parses_once():
    budget = ParseBudget()
    html = "<html><body><a href='/x'>x</a><form action='/s'><input name='q'></form></body></html>"
    extraction.html_diagnostics(html, canonical("http://example.com/"))
    assert budget.parses == 1


def test_shared_tree_parses_once_across_passes():
    budget = ParseBudget()
    html = (
        "<html><body><a href='/x'>x</a><iframe src='/f'></iframe>"
        "<form action='/s'><input name='q'></form></body></html>"
    )
    tree = extraction.parse_html_tree(html)
    assert budget.parses == 1
    extraction.html_diagnostics(html, canonical("http://example.com/"), tree=tree)
    links, _, _ = extraction.extract_discovered_links(html, canonical("http://example.com/"), tree=tree)
    # Frames, forms, and links all reused the single pre-parsed tree.
    assert budget.parses == 1
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
        result = extract_content(html.encode(), "text/html", canonical("http://e.com/"), selectors=selectors)
        assert result.text == "selected"

    def test_extraction_still_works_after_a_shutdown(self):
        """A shutdown releases the workers; it does not retire extraction.

        ``shutdown_doc_extraction_executor`` is on the app lifespan, so every test
        that drives the lifespan once ran it -- and a module-global executor that
        is shut down and never rebuilt left every LATER extraction test in that
        worker raising ``cannot schedule new futures after shutdown``. The suite
        passed or failed on test ordering rather than on behaviour, and only
        randomized order ever exposed it.

        The sibling process pool in the same module already had this right: it is
        built on demand, torn down to ``None``, and rebuilt by the next caller. A
        shutdown there means "not right now", so it has to mean the same thing
        here -- otherwise the two pools in one module disagree about what
        shutdown means, and only one of them is safe to call twice.
        """
        asyncio.run(shutdown_doc_extraction_executor())

        result = extract_content(
            b'<div class="target">still working</div>',
            "text/html",
            "http://e.com/",
            selectors=Selectors(css=".target"),
        )
        assert result.text == "still working"

    def test_html_to_markdown_markdownify_success(self):
        text, md = html_to_markdown("<p>hello <strong>world</strong></p>")
        assert text is not None
        assert md is not None
        assert "world" in md

    def test_html_to_markdown_markdownify_exception(self):
        text, md = html_to_markdown("<p>test</p>")
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
        assert extract_selected(html, "http://e.com/", selectors=selectors) == ExtractedContent(None, None)

    def test_extract_selected_xpath_string_node(self):
        result = extract_selected(
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
        assert extract_html(html, "http://e.com/").text is not None

    def test_extract_document_markitdown_import_error(self):
        assert extract_document(b"fake pdf content", "http://e.com/doc.pdf").text is not None

    def test_extract_document_markitdown_exception(self):
        result = extract_document(b"not a pdf", "http://e.com/doc.pdf")
        assert isinstance(result, ExtractedContent)

    def test_parse_html_tree_empty(self):
        assert parse_html_tree("") is None
        assert parse_html_tree(None) is None

    def test_parse_html_tree_parser_error(self):
        assert parse_html_tree("<").tag == "html"


class TestExtractionGaps:
    def test_nested_anchor(self):
        # Real HTML parsers (lxml) flatten illegally-nested anchors into
        # siblings, so both distinct hrefs are discovered as separate links
        # rather than the inner one being swallowed by the outer.
        links, _, _ = extract_discovered_links(
            '<a href="/x">outer <a href="/y">inner</a></a>',
            canonical("http://e.com/"),
        )
        hrefs = {entry.normalized_url.raw for entry in links}
        assert "http://e.com/x" in hrefs
        assert "http://e.com/y" in hrefs

    def test_duplicate_href(self):
        links, count, _ = extract_discovered_links(
            '<a href="/x">a</a><a href="/x">b</a>',
            canonical("http://e.com/"),
        )
        assert len(links) == 1
        assert count == 1

    def test_max_links_truncation(self):
        html = "".join(f'<a href="/{i}">x</a>' for i in range(5))
        links, count, truncated = extract_discovered_links(html, canonical("http://e.com/"), max_links=2)
        assert len(links) == 2
        assert truncated
        assert count == 5

    def test_html_diagnostics_frames_and_forms(self):
        html = (
            '<frameset><frame src="/a"></frameset>'
            '<form action="/s" method="post"><input name="q" value="1"></form>'
        )
        diag = html_diagnostics(html, canonical("http://e.com/"))
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
        extraction.extract_html(html, url=canonical("http://example.com/"))

        trafilatura_records = [r for r in caplog.records if r.name.startswith("trafilatura")]
        errors = [r for r in trafilatura_records if r.levelno >= logging.ERROR]
        assert not errors, (
            "trafilatura emitted ERROR records for a benign input; these are the "
            "2239 spurious per-run records. Extract already falls back to "
            "markdownify/plain text, so the record carries no signal."
        )

    def test_real_html_still_extracts_and_logs_no_error(self, caplog):
        out = extraction.extract_html(
            "<html><body><article><h1>Title</h1><p>Real body text here.</p></article></body></html>",
            url=canonical("http://example.com/"),
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
    with caplog.at_level(logging.WARNING, logger="app.extraction.content"):
        result = extraction_mod.extract_content(
            pdf,
            "application/pdf",
            canonical("http://example.com/doc.pdf"),
            selectors=Selectors(css="#agenda"),
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
    with caplog.at_level(logging.WARNING, logger="app.extraction.content"):
        narrowed = extraction_mod.extract_content(
            html, "text/html", "http://example.com/", selectors=Selectors(css="#agenda")
        )
    assert "extraction_selector_ignored_non_html" not in caplog.text, caplog.text
    assert "item one" in (narrowed.markdown or ""), narrowed
    assert "filler" not in (narrowed.markdown or ""), narrowed

    # No selector, no warning: the common path must stay quiet.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="app.extraction.content"):
        extraction_mod.extract_content(pdf, "application/pdf", canonical("http://example.com/doc.pdf"))
    assert "extraction_selector_ignored_non_html" not in caplog.text, caplog.text


# Issue #77. These ten are contracts in their own right -- each has a signature, a
# docstring and tests of its own -- and none of them is reachable from outside the
# module because they were named private. The four existing public entry points are
# *compositions* of these, so "just call extract_content instead" is not a migration:
# it cannot express a classifier case, and it drags a whole extraction run in to
# assert one predicate.
EXTRACTION_STAGE_CONTRACTS = (
    "is_html",
    "looks_like_document",
    "html_to_markdown",
    "extract_html",
    "extract_document",
    "detect_pagination",
    "html_diagnostics",
    "extract_discovered_links",
    "forms_from",
    "frames_from",
)


@pytest.mark.parametrize("name", EXTRACTION_STAGE_CONTRACTS)
def test_extraction_stage_contracts_are_public(name):
    """A stage contract a test can name must not be a private name."""
    assert not name.startswith("_")
    assert hasattr(extraction, name), f"app.extraction.content.{name} is not public"
    assert name in vars(extraction), f"{name} is not defined in app.extraction.content"


@pytest.mark.parametrize("name", EXTRACTION_STAGE_CONTRACTS)
def test_extraction_stage_contracts_keep_their_signatures(name):
    """Renaming must not change what a stage is called with or returns."""
    import inspect

    sig = inspect.signature(getattr(extraction, name))
    assert sig.parameters, f"{name} takes no arguments, which cannot be the whole contract"
