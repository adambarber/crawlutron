"""Slow selectors time out; shared trees parse once."""

import logging
import threading
from contextlib import contextmanager

import crawlutron.extraction.content as extraction
from crawlutron.extraction import documents as docex
from crawlutron.extraction.content import ExtractedContent, Selectors, extract_selected
from tests._extraction_helpers import settings_with
from tests._parse_budget import ParseBudget
from tests._policy_factories import canonical


def assert_selector_timed_out(result, caplog, flavour: str) -> None:
    """A selector that timed out and one that matched nothing look identical.

    Both yield ``ExtractedContent(None, None)``, so only the warning
    distinguishes a working deadline from a silent one.
    """
    assert result == ExtractedContent(None, None)
    assert "extraction_selector_timeout" in caplog.text, (
        f"the {flavour} selector returned empty rather than timing out: the "
        "extraction selector deadline did not fire"
    )


@contextmanager
def _parked_stub_worker():
    """Yield an Event parking a selector stub, always released on exit.

    The deadline abandons the worker thread inside the stub; releasing here
    keeps the executor shutdown from paying the full 30s wait (#566).
    """
    event = threading.Event()
    try:
        yield event
    finally:
        event.set()


def _timed_out_selector(monkeypatch, caplog, attr, selectors, flavour: str) -> None:
    """Parked ``docex.<attr>`` evaluation must time out under the selector deadline."""
    html = "<html><body><p class='x'>hi</p></body></html>"
    with _parked_stub_worker() as never_released:
        monkeypatch.setattr(docex, attr, lambda root, arg: never_released.wait(timeout=30) or [])
        with caplog.at_level(logging.WARNING, logger="crawlutron.extraction.content"):
            result = extract_selected(
                html,
                canonical("http://example.com/"),
                selectors=selectors,
                settings=settings_with(extraction_selector_timeout=0.2),
            )
    assert_selector_timed_out(result, caplog, flavour)


def test_css_selector_slow_must_not_hang(monkeypatch, caplog):
    """A selector that never returns must be bounded, not awaited.

    The stub blocks on an Event nothing ever sets, so the only thing that can
    end the wait is the selector deadline firing inside ``_eval_selector``.
    """
    _timed_out_selector(monkeypatch, caplog, "find_all", Selectors(css=".x"), "CSS")


def test_css_selector_fast_still_works():
    html = "<html><body><p class='x'>hi</p></body></html>"
    result = extract_selected(html, canonical("http://example.com/"), selectors=Selectors(css=".x"))
    assert result.text is not None
    assert "hi" in (result.text or "")


def test_xpath_selector_slow_must_not_hang(monkeypatch, caplog):
    """The XPath twin of the CSS test: same instrument, same warning assertion.

    The parked evaluation is crawlutron_extract.xpath_eval.select, bounded by
    documents._eval_selector (the surviving owner of bounded selector evaluation).
    """
    _timed_out_selector(monkeypatch, caplog, "select", Selectors(xpath="//p"), "XPath")


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
