"""XPath subset evaluator validated against the frozen golden corpus.

The corpus is generated once by ``tests/fixtures/generate_xpath_golden.py``
and committed as ``tests/fixtures/xpath_golden.json``; the test runs the evaluator against frozen triples.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from crawlutron_extract.html_dom import build_dom
from crawlutron_extract.xpath_eval import select, select_values
from crawlutron_extract.xpath_select import SelectorError

FIXTURE = pathlib.Path(__file__).resolve().parent / "fixtures" / "xpath_golden.json"


def _load_corpus() -> list[tuple[str, str, list[str]]]:
    with FIXTURE.open(encoding="utf-8") as fh:
        return [(row[0], row[1], row[2]) for row in json.load(fh)]


@pytest.mark.parametrize("html,xpath,expected", _load_corpus())
def test_xpath_matches_golden_corpus(html: str, xpath: str, expected: list[str]) -> None:
    """Every corpus triple evaluates to the lxml-computed expected value."""
    dom, _ = build_dom(html, 1000, 50)
    assert dom is not None
    if xpath.strip().endswith("/text()") or "/@" in xpath:
        got = select_values(dom, xpath)
    else:
        got = [node.tag for node in select(dom, xpath)]
    assert got == expected


def test_selector_outside_grammar_raises() -> None:
    """Any expression outside the grammar raises SelectorError."""
    dom, _ = build_dom("<p>x</p>", 1000, 50)
    assert dom is not None
    for bad in [
        "//following-sibling::p",
        "//count(//p)",
        "//p[",
        "//@",
        "xyz@",
    ]:
        with pytest.raises(SelectorError):
            select(dom, bad)


def test_selector_matching_nothing_returns_empty() -> None:
    """A selector that matches nothing yields an empty list, never a raise."""
    dom, _ = build_dom("<p>x</p>", 1000, 50)
    assert dom is not None
    assert select(dom, "//span") == []
    assert select_values(dom, "//span/text()") == []
    assert select_values(dom, "//p/@nope") == []
