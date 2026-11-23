"""HTML markdown rendering: emphasis, lists, blockquotes, images, tables.

Split from test_html_pass.py at the file-size ceiling; assertions unchanged.
"""

from __future__ import annotations

import pytest

from crawlutron_extract import Quality, extract

_TABLE_WITH_NESTED = (
    b"<table><tr><td>Outer before <table><tr><td>nested</td></tr></table> Outer after</td></tr></table>"
)
_TABLE_WITH_ROWSPAN = b'<table><tr><td rowspan="2">spanning</td><td>b</td></tr><tr><td>c</td></tr></table>'


@pytest.mark.parametrize(
    ("html", "fragments"),
    [
        (b"<p><strong>bold</strong></p>", ("**bold**",)),
        (b"<p><em>italic</em></p>", ("*italic*",)),
        (b"<p><code>fmt</code></p>", ("`fmt`",)),
        (b"<p><strong>a <em>b</em> c</strong></p>", ("**a *b* c**",)),
        (b"<blockquote>quoted</blockquote>", ("> quoted",)),
        (
            b'<img alt="An image" src="https://example.com/img.png">',
            ("![An image](https://example.com/img.png)",),
        ),
        (b"<p>a</p><hr><p>b</p>", ("---\n",)),
        (b"<ul><li>outer<ul><li>inner</li></ul></li></ul>", ("- outer", "  - inner")),
        (b'<ol start="4"><li>a</li><li>b</li></ol>', ("4. a", "5. b")),
        (
            b"<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>",
            ("| A | B |", "|---|---|", "| 1 | 2 |"),
        ),
        (b"<table><caption>Summary</caption><tr><td>1</td></tr></table>", ("Summary",)),
        (b'<table><tr><td colspan="2">merged</td></tr></table>', ("merged",)),
        (_TABLE_WITH_ROWSPAN, ()),
    ],
)
def test_markdown_renders_expected_fragments(html: bytes, fragments: tuple[str, ...]) -> None:
    result = extract(html, "text/html")
    assert isinstance(result.markdown, str)
    for fragment in fragments:
        assert fragment in result.markdown


@pytest.mark.parametrize(
    ("html", "flag"),
    [
        (
            b"<table><tr><td><table><tr><td>nested</td></tr></table></td></tr></table>",
            "nested_table_flattened",
        ),
        (_TABLE_WITH_ROWSPAN, "table_grid_degraded"),
    ],
)
def test_table_degradation_flag(html: bytes, flag: str) -> None:
    assert flag in extract(html, "text/html").flags


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        (_TABLE_WITH_NESTED, [[["Outer before nested Outer after"]]]),
        (_TABLE_WITH_ROWSPAN, [[["spanning", "b"], ["spanning", "c"]]]),
    ],
)
def test_table_grid_preserved(html: bytes, expected: list) -> None:
    assert extract(html, "text/html").tables == expected


def test_quality_medium_paths_distinguishable() -> None:
    no_main = extract(b"<p>Some body text without a main content marker.</p>", "text/html")
    assert no_main.quality == Quality.MEDIUM
    below_floor = extract(b"<main>" + b"x" * 199 + b"</main>", "text/html")
    assert below_floor.quality == Quality.MEDIUM


__all__ = [
    "test_markdown_renders_expected_fragments",
    "test_quality_medium_paths_distinguishable",
    "test_table_degradation_flag",
    "test_table_grid_preserved",
]
