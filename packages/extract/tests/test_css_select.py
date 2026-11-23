"""CSS tokenizer/validator: every input validates or raises, never hangs.

Refused inputs name the offending token; the cursor advances each iteration.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from crawlutron_extract.css_select import (
    SelectorError,
    find_all,
    matches_chain,
    parse_selector_list,
    validate_css,
)
from crawlutron_extract.html_dom import DOMNode

REFUSED = ["//a[", "'unmatched", '"unmatched']
ACCEPTED = ["[a^='v']", "[a$='v']", "[a*='v']", "[a='v']", '[a="v"]', "[a=v]", "[a]"]


def _validate_bounded(selector: str, timeout: float = 5.0) -> None:
    """Run validation in a worker thread so a hang is a bounded failure."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(validate_css, selector)
        future.result(timeout=timeout)


@pytest.mark.parametrize("selector", REFUSED)
def test_refused_selector_never_hangs(selector: str) -> None:
    """Every refused input raises SelectorError within the time bound."""
    with pytest.raises(SelectorError):
        _validate_bounded(selector)


def test_unmatched_quote_names_itself() -> None:
    """An unmatched quote names itself as the offending token."""
    with pytest.raises(SelectorError) as exc:
        _validate_bounded("'unmatched")
    assert "'" in str(exc.value)


def test_attribute_operators_accepted() -> None:
    """Equality, prefix, suffix and contains operators all validate."""
    for selector in ACCEPTED:
        validate_css(selector)


def test_compound_selector_splits_type_and_class() -> None:
    """``span.price`` tokenizes as type + class, not one fused ident."""
    for selector, type_name, class_name in [
        ("span.price", "span", "price"),
        ("article.product", "article", "product"),
    ]:
        chains = parse_selector_list(selector)
        parts = chains[0][0][0]
        assert ("TYPE", type_name) in parts
        assert ("CLASS", class_name) in parts


def _chain(selector: str):
    return parse_selector_list(selector)[0]


def _nested_dom() -> DOMNode:
    div = DOMNode("div", {}, None)
    article = DOMNode("article", {}, div)
    div.add_child(article)
    section = DOMNode("section", {}, article)
    article.add_child(section)
    p = DOMNode("p", {}, section)
    section.add_child(p)
    return div


def _five_ps() -> DOMNode:
    parent = DOMNode("div", {}, None)
    for _ in range(5):
        child = DOMNode("p", {}, parent)
        parent.add_child(child)
    return parent


def _nth_matched_indices(parent: DOMNode, an_b: str) -> list[int]:
    chain = _chain(f"p:nth-child({an_b})")
    return [index for index, child in enumerate(parent.children, start=1) if matches_chain(child, chain)]


@pytest.mark.parametrize(
    "selector,expected",
    [
        ("article p", {"p"}),
        ("article section p", {"p"}),
        ("div  p", {"p"}),
        ("div p ", {"p"}),
        ("article > p", set()),
        ("p article", set()),
    ],
)
def test_descendant_combinator(selector: str, expected: set[str]) -> None:
    """Whitespace is a descendant combinator; child and inverted forms do not match."""
    matched = {node.tag for node in find_all(_nested_dom(), _chain(selector))}
    assert matched == expected


def test_descendant_validates() -> None:
    """Whitespace-separated chains validate instead of raising 'Trailing tokens'."""
    validate_css("div p")
    validate_css("article section p")
    validate_css("div  p ")


@pytest.mark.parametrize(
    "an_b,expected",
    [
        ("odd", [1, 3, 5]),
        ("even", [2, 4]),
        ("2n+1", [1, 3, 5]),
        ("2n", [2, 4]),
        ("3", [3]),
        ("1", [1]),
        ("-n+3", [1, 2, 3]),
        ("n", [1, 2, 3, 4, 5]),
    ],
)
def test_nth_child_an_plus_b(an_b: str, expected: list[int]) -> None:
    """An+B forms map to the right 1-based child indices."""
    assert _nth_matched_indices(_five_ps(), an_b) == expected


def test_nth_child_plain_integer_preserved() -> None:
    """The existing plain-integer :nth-child(2) still matches the second child."""
    assert _nth_matched_indices(_five_ps(), "2") == [2]


@pytest.mark.parametrize("an_b", ["2n+", "n+", "abc", "", "+", "-"])
def test_nth_child_malformed_raises(an_b: str) -> None:
    """Malformed An+B raises SelectorError, never a raw ValueError or IndexError."""
    with pytest.raises(SelectorError):
        parse_selector_list(f"p:nth-child({an_b})")


@pytest.mark.parametrize(
    "selector",
    ["p:nth-child(odd)", "p:nth-child(2n+1)", "p:nth-child(-n+3)", "p:nth-child(3)"],
)
def test_nth_child_validates(selector: str) -> None:
    """Well-formed An+B arguments validate through the public entry point."""
    validate_css(selector)


@pytest.mark.parametrize("selector", ["p:nth-child(2n+)", "p:nth-child(abc)", "p:nth-child()"])
def test_nth_child_refused(selector: str) -> None:
    """Malformed :nth-child() arguments are refused as SelectorError."""
    with pytest.raises(SelectorError):
        validate_css(selector)
