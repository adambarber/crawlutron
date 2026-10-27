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
from app.extraction.doc_pool import (
    DocExtractionPool,
    install_doc_pool_hooks,
    spawn_doc_worker,
    terminate_doc_process,
)
from app.vocabulary.limits import DEFAULT_EXTRACTION_MAX_MARKDOWN
from tests._assertions import set_optional_env
from tests._extraction_helpers import doc_worker_acquisitions, extract_discovered_links, settings_with
from tests._policy_factories import BOOL_ENV_CASES, canonical
from tests._process_doubles import StubSpawner, stub_worker
from tests.extraction.test_extraction import assert_extraction_empty


@pytest.mark.parametrize(("raw", "expected"), BOOL_ENV_CASES)
def test_document_worker_gate_uses_the_shared_bool_parser(monkeypatch, caplog, raw, expected):
    """``CRAWLUTRON_EXTRACTION_DOC_ENABLED`` is read by the shared env parser.

    "on" must mean the same here as in every flag; asserting through the pool
    proves the reader is the one production uses."""
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

    Re-derived rather than imported because the reader is private; this test is
    about the parser's verdict, and the pool honoring it is asserted above."""
    return parse_bool_env("CRAWLUTRON_EXTRACTION_DOC_ENABLED", default=True)


def test_a_spawned_worker_is_ready_before_it_is_handed_out():
    """The spawner waits for the child's readiness.

    The child imports markitdown before it can serve, so the parent must not
    hand the worker out before that finishes."""
    worker = spawn_doc_worker(30.0)
    try:
        assert worker.proc.is_alive(), "the worker process did not start"
        assert worker.conn.poll(0) is False, "the readiness signal was left in the pipe"
    finally:
        terminate_doc_process(worker.proc)


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
def _reset_doc_extraction_pool(monkeypatch):
    """Hand every test a fresh, isolated pool (teardown is terminal now, not a reset).

    Each extraction spawns its own short-lived worker (reaped on completion or
    timeout); the injected env flags revert via ``monkeypatch`` teardown."""
    from tests._doc_pool_fixtures import fresh_doc_pool

    fresh_doc_pool(monkeypatch)
    yield


async def test_extract_document_timeout_is_enforced(monkeypatch):
    """A document extraction exceeding the wall-clock timeout returns None.

    The hang lives in the worker SUBPROCESS, injected via the hooks seam, so
    the suite's in-process sleep patch is irrelevant -- no real sleeps needed."""
    # The worker subprocess hangs; a deadline short enough to notice is the whole point of the test, so it is
    # passed in rather than written onto the module the call lives in.
    install_doc_pool_hooks({"hang": True})
    try:
        settings = settings_with(extraction_doc_timeout=0.01)

        assert_extraction_empty(
            extract_document(b"%PDF-1.4 fake", "http://example.com/x.pdf", settings=settings)
        )
    finally:
        install_doc_pool_hooks(None)


async def test_extract_document_child_death_returns_empty(monkeypatch):
    """A worker that dies before sending (segfault) yields empty extraction,
    not an unhandled EOFError, and leaves no zombie behind."""
    # Isolate the pool this call drives: the child-death contract is per worker,
    # so this test's pool must hold only this call's own worker.
    install_doc_pool_hooks({"die": True})
    try:
        assert_extraction_empty(extract_document(b"%PDF-1.4 fake", "http://example.com/x.pdf"))
    finally:
        install_doc_pool_hooks(None)

    # The only pooled worker is the one THIS call drove: after its child death it
    # must be reaped (not a zombie) and never left marked busy mid-task.
    for alive, reaped, busy in ext_mod.DOC_POOL.health():
        assert not busy, "worker left marked busy after a child death"
        assert alive or reaped, "zombie worker left in the pool after a child death"


async def test_extract_document_output_is_truncated_at_the_configured_cap(monkeypatch):
    """Extracted markdown is truncated to the cap the caller configured.

    Same emission hook as the other scenarios; 10 KB against a 10 KB cap is a
    truncation, visible without materializing the 5 MB default."""
    cap = 10000
    install_doc_pool_hooks({"output": "x" * (cap + 1000)})
    try:
        result = extract_document(
            b"%PDF-1.4 fake", "http://example.com/x.pdf", settings_with(extraction_max_markdown=cap)
        )
    finally:
        install_doc_pool_hooks(None)

    assert result.markdown is not None
    assert len(result.markdown) == cap, "the document exceeded the configured cap"
    assert cap < DEFAULT_EXTRACTION_MAX_MARKDOWN, "a cap at the default proves nothing"


async def test_extract_document_normal_works():
    """A real document comes back as real markdown, converted by a real worker.

    No hook: the echo path is covered elsewhere, and only a real conversion
    exercises the parent/child request-reply shape."""
    result = extract_document(b"name,score\nada,10\n", "http://example.com/data.csv")

    assert result.markdown is not None
    # "ada" only reaches the reply if markitdown really read these bytes, and it only gets read in a worker
    # subprocess.
    assert "ada" in result.markdown
    # A real spawn serving under xdist can abort natively (libc++abi recursive_mutex), so the raw pid
    # registry is transient; the durable contract is that no worker is left marked busy mid-task.
    for alive, reaped, busy in ext_mod.DOC_POOL.health():
        assert not busy, "a real conversion left a worker marked busy"
        assert alive or reaped, "a real conversion left a zombie worker"


# ============================================================ Document extraction runs in a process that is
# forcefully killed on timeout (no lingering zombie / stalled pool).


async def test_extract_document_process_pool_isolation(monkeypatch):
    """Document extraction runs in a separate process so a hang can be killed.

    The kill and the hang are injected into the worker SUBPROCESS via the
    hooks seam, so the in-process sleep patch never needed to apply here."""
    install_doc_pool_hooks({"hang": True})
    try:
        result = extract_document(
            b"%PDF-1.4 fake",
            "http://example.com/x.pdf",
            settings=settings_with(extraction_doc_timeout=0.01),
        )
    finally:
        install_doc_pool_hooks(None)
    # Should return None due to timeout (process killed).
    assert result.text is None
    assert result.markdown is None


async def test_extract_document_process_pool_normal_works():
    """Normal document extraction still works with process isolation."""
    from app.extraction.content import extract_document

    # No hooks installed - normal operation.
    result = extract_document(b"%PDF-1.4 fake", "http://example.com/x.pdf")
    # With fake bytes, markitdown returns something (or None for unparseable); the key is it doesn't crash.
    assert isinstance(result, ext_mod.ExtractedContent)


async def test_extract_document_recovers_after_timeout(monkeypatch):
    """After a timed-out (killed) extraction, the next call still works.

    A killed worker must not leave a zombie that stalls
    the (semaphore-bounded) extraction concurrency.
    """
    # First call hangs and is killed on timeout (short timeout, hang injected).
    install_doc_pool_hooks({"hang": True})
    try:
        hung = extract_document(
            b"%PDF-1.4 fake",
            "http://example.com/hang.pdf",
            settings=settings_with(extraction_doc_timeout=0.01),
        )
    finally:
        install_doc_pool_hooks(None)
    assert hung.text is None

    # Second call (same process / shared semaphore) must succeed.
    install_doc_pool_hooks({"output": "## Recovered"})
    try:
        recovered = extract_document(
            b"%PDF-1.4 fake",
            "http://example.com/ok.pdf",
            settings=settings_with(extraction_doc_timeout=10.0),
        )
    finally:
        install_doc_pool_hooks(None)
    assert recovered.markdown == "## Recovered"


async def test_test_hooks_inert_without_seam(monkeypatch):
    """Without the hooks seam armed, nothing answers -- a stray env value must
    never reach a production child now the env gate is deleted."""
    monkeypatch.setenv("TEST_MARKITDOWN_OUTPUT", "## hijacked")

    ungated = extract_document(b"%PDF-1.4 fake", "http://example.com/x.pdf")
    assert ungated.markdown != "## hijacked"

    install_doc_pool_hooks({"output": "## hijacked"})
    try:
        gated = extract_document(b"%PDF-1.4 fake", "http://example.com/x.pdf")
    finally:
        install_doc_pool_hooks(None)
    assert gated.markdown == "## hijacked"
