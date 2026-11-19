"""CSS tokenizer/validator: every input validates or raises, never hangs.

Refused inputs name the offending token; the cursor advances each iteration.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from crawlutron_extract.css_select import SelectorError, parse_selector_list, validate_css

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
