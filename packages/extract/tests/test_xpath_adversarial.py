"""Adversarial tests for the XPath subset evaluator.

Malformed expressions, unsupported axes/functions, unterminated predicates,
the DOM node cap, unicode and entity-decoded attribute values, empty results.
"""

from __future__ import annotations

import pytest
from crawlutron_extract.html_dom import build_dom
from crawlutron_extract.xpath_eval import select, select_values
from crawlutron_extract.xpath_select import SelectorError


def _dom(html: str, max_nodes: int = 1000):
    return build_dom(html, max_nodes, 50)[0]


@pytest.mark.parametrize(
    "bad",
    [
        "//following-sibling::p",  # unsupported axis
        "//count(//p)",  # unsupported function
        "//ancestor::div",  # unsupported axis
        "//p[",  # unterminated predicate
        "//p[@",  # unterminated attribute predicate
        "//p[@id=",  # unterminated attribute value
        "//div[contains(@class, 'x]",  # unterminated string literal
        "xyz@",  # stray @
        "//p[@]",  # bare @ with no name
        "//text",  # text without parens
    ],
)
def test_malformed_expressions_raise(bad):
    dom = _dom("<p>x</p>")
    with pytest.raises(SelectorError):
        select(dom, bad)


@pytest.mark.parametrize(
    "bad,token",
    [
        ("//following-sibling::p", ":"),  # unsupported axis char
        ("//count(//p)", "("),  # count node test leaves ( unparsed
        ("xyz@", "@"),  # stray @
        ("//p[@]", "]"),  # bare @ followed by ]
    ],
)
def test_malformed_expressions_name_the_offending_token(bad, token):
    dom = _dom("<p>x</p>")
    with pytest.raises(SelectorError) as exc:
        select(dom, bad)
    assert exc.value.token == token


def test_unterminated_predicates_raise():
    dom = _dom("<p>x</p>")
    for bad in ["//p[", "//p[@", "//p[last()", "//p[1"]:
        with pytest.raises(SelectorError):
            select(dom, bad)


def test_huge_dom_respects_max_dom_nodes():
    """A //* over a huge document is bounded by max_dom_nodes via build_dom."""
    many = "".join(f'<p id="{i}">{i}</p>' for i in range(10_000))
    dom, truncated = build_dom(many, max_nodes=100, max_depth=50)
    assert dom is not None
    matched = select(dom, "//*")
    assert len(matched) <= 100
    # The build was truncated by the node cap.
    assert truncated is True


def test_unicode_attribute_values():
    dom = _dom('<div class="café"><span class="naïve">x</span></div>')
    assert [n.tag for n in select(dom, "//div[@class='café']")] == ["div"]
    assert [n.tag for n in select(dom, "//span[@class='naïve']")] == ["span"]


def test_entity_decoded_attribute_values():
    """convert_charrefs decodes entities, so the decoded value matches."""
    dom = _dom('<a href="x&amp;y">link</a>')
    assert select_values(dom, "//a/@href") == ["x&y"]
    assert [n.tag for n in select(dom, "//a[@href='x&y']")] == ["a"]


def test_selector_matching_nothing_returns_empty():
    dom = _dom("<p>x</p>")
    assert select(dom, "//span") == []
    assert select(dom, "//p[@id='nope']") == []
    assert select_values(dom, "//span/text()") == []
    assert select_values(dom, "//p/@missing") == []
