"""Slow selectors time out; shared trees parse once."""

import logging
import threading

import bs4

import app.extraction.content as extraction
from app.extraction.content import ExtractedContent, Selectors, extract_selected
from tests._extraction_helpers import settings_with
from tests._parse_budget import ParseBudget
from tests._policy_factories import canonical


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
