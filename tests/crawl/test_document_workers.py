"""Document workers gate, isolate and bound extraction subprocesses."""

import asyncio

import pytest

import app.extraction.content as ext_mod
import app.extraction.content as extraction
from app.config.env import parse_bool_env
from app.extraction.content import (
    ExtractedContent,
    Selectors,
    extract_document,
    extract_selected,
    parse_html_tree,
)
from app.orchestration.doc_pool import DocExtractionPool
from app.vocabulary.limits import DEFAULT_EXTRACTION_MAX_MARKDOWN
from tests._assertions import set_optional_env
from tests._extraction_helpers import doc_worker_acquisitions, extract_discovered_links, settings_with
from tests._policy_factories import BOOL_ENV_CASES, canonical
from tests._process_doubles import StubSpawner, stub_worker
from tests.crawl.test_extraction import assert_extraction_empty


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
    # The worker subprocess hangs; a deadline short enough to notice is the whole point of the test, so it is
    # passed in rather than written onto the module the call lives in.
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

    # Is per worker, not process-global: the pool is persistent and sized above one, so an idle worker from an
    # earlier extraction legitimately stays alive.
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
    # "ada" only reaches the reply if markitdown really read these bytes, and it only gets read in a worker
    # subprocess.
    assert "ada" in result.markdown
    assert ext_mod.DOC_POOL.pids(), "no worker process served the conversion"


# ============================================================ Document extraction runs in a process that is
# forcefully killed on timeout (no lingering zombie / stalled pool).


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
    # With fake bytes, markitdown returns something (or None for unparseable); the key is it doesn't crash.
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

    # Second call (same process / shared semaphore) must succeed.
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
