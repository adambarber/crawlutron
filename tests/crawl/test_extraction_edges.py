"""Extraction edges: gaps, chatter suppression and stage contracts."""

import asyncio
import logging

import pytest

import app.extraction.content as extraction
from app.extraction.content import (
    ExtractedContent,
    Selectors,
    extract_document,
    extract_html,
    extract_selected,
    html_to_markdown,
    parse_html_tree,
    shutdown_doc_extraction_executor,
)
from tests._extraction_helpers import (
    extract_content,
    extract_discovered_links,
    extract_js_endpoints,
    html_diagnostics,
)
from tests._policy_factories import canonical


@pytest.fixture(autouse=True)
def _isolated_doc_pool(monkeypatch):
    """Hand every test a fresh, isolated doc pool.

    ``teardown()``/``shutdown_doc_extraction_executor()`` are terminal since #246;
    a fresh pool per test keeps those shutdown calls from poisoning the shared one."""
    from tests.crawl._doc_pool_fixtures import fresh_doc_pool

    fresh_doc_pool(monkeypatch)
    yield


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

        The module-global executor, shut down and never rebuilt, left later
        extractions raising ``cannot schedule new futures after shutdown``.
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
        # Real HTML parsers (lxml) flatten illegally-nested anchors into siblings, so both distinct hrefs are
        # discovered as separate links rather than the inner one being swallowed by the outer.
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

    trafilatura logs ERROR for empty/fragment/no-main HTML -- normal crawler
    conditions it already falls through on -- crowding out real failures.
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

    ``extract_content`` routes the selector pair only on the HTML branch, so a
    PDF returns the full content; the warning is the load-bearing assertion.
    """
    pdf = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n"
    with caplog.at_level(logging.WARNING, logger="app.extraction.content"):
        result = extraction.extract_content(
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

    # The same call against HTML does narrow, and so must NOT warn -- otherwise this passes by warning
    # unconditionally and proves nothing.
    caplog.clear()
    html = "<html><body><div id='agenda'>item one</div><p>filler</p></body></html>"
    with caplog.at_level(logging.WARNING, logger="app.extraction.content"):
        narrowed = extraction.extract_content(
            html, "text/html", "http://example.com/", selectors=Selectors(css="#agenda")
        )
    assert "extraction_selector_ignored_non_html" not in caplog.text, caplog.text
    assert "item one" in (narrowed.markdown or ""), narrowed
    assert "filler" not in (narrowed.markdown or ""), narrowed

    # No selector, no warning: the common path must stay quiet.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="app.extraction.content"):
        extraction.extract_content(pdf, "application/pdf", canonical("http://example.com/doc.pdf"))
    assert "extraction_selector_ignored_non_html" not in caplog.text, caplog.text


# Issue #77.
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
