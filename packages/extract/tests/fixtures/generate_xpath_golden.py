"""One-off build-time generator for the XPath golden corpus.

Emits ``tests/fixtures/xpath_golden.json`` (triples from lxml); committed as a frozen runtime fixture.
"""

from __future__ import annotations

import json
import pathlib

from lxml import html as lxml_html

FIXTURE = pathlib.Path(__file__).resolve().parent / "xpath_golden.json"

TRIPLES: list[tuple[str, str, list[str]]] = []


def add(html: str, xpath: str, expected: list[str]) -> None:
    TRIPLES.append((html, xpath, expected))


# Shared reference document.
DOC = (
    '<div class="a b">'
    '<p id="x">hello</p>'
    '<p class="x">world</p>'
    "<section><div><span>deep</span></div></section>"
    "</div>"
)


def lxml_values(html: str, xpath: str) -> list[str]:
    """Evaluate ``xpath`` over the document body with lxml, as strings.

    Node results report the element tag; ``text()`` / ``@attr`` results report
    the string value. This mirrors what the subset evaluator produces.
    """
    tree = lxml_html.fromstring("<html><body>" + html + "</body></html>")
    body = tree.find("body")
    assert body is not None
    result = body.xpath(xpath)
    stripped = xpath.strip()
    is_value = stripped.endswith("/text()") or "/@" in stripped
    out: list[str] = []
    for item in result:
        if isinstance(item, str):
            out.append(item)
        elif is_value:
            out.append(str(item))
        else:
            out.append(item.tag)
    return out


# --- name tests and * -------------------------------------------------------
add(DOC, "//p", lxml_values(DOC, "//p"))
add(DOC, "//div", lxml_values(DOC, "//div"))
add(DOC, "//span", lxml_values(DOC, "//span"))  # matches nothing -> []

# --- positional predicates --------------------------------------------------
add(DOC, "//p[1]", lxml_values(DOC, "//p[1]"))
add(DOC, "//p[last()]", lxml_values(DOC, "//p[last()]"))

# --- attribute predicates ---------------------------------------------------
add(DOC, "//p[@id]", lxml_values(DOC, "//p[@id]"))
add(DOC, "//p[@id='x']", lxml_values(DOC, "//p[@id='x']"))
add(DOC, "//p[@class='x']", lxml_values(DOC, "//p[@class='x']"))

# --- functions --------------------------------------------------------------
add(DOC, "//p[contains(@class, 'x')]", lxml_values(DOC, "//p[contains(@class, 'x')]"))
add(DOC, "//p[starts-with(@id, 'x')]", lxml_values(DOC, "//p[starts-with(@id, 'x')]"))
add(
    DOC,
    "//*[contains(concat(' ', normalize-space(@class), ' '), ' b ')]",
    lxml_values(DOC, "//*[contains(concat(' ', normalize-space(@class), ' '), ' b ')]"),
)

# --- node results: text() and @attr -----------------------------------------
add(DOC, "//p/text()", lxml_values(DOC, "//p/text()"))
add(DOC, "//p[1]/text()", lxml_values(DOC, "//p[1]/text()"))
add(DOC, "//div/@class", lxml_values(DOC, "//div/@class"))
add(DOC, "//p/@id", lxml_values(DOC, "//p/@id"))

# --- combinators ------------------------------------------------------------
add(DOC, "//div/p", lxml_values(DOC, "//div/p"))
add(DOC, "//div//p", lxml_values(DOC, "//div//p"))
add(DOC, ".//p", lxml_values(DOC, ".//p"))

# --- deep descendant --------------------------------------------------------
DEEP = '<article><section><div><p><span id="t">target</span></p></div></section></article>'
add(DEEP, "//article/section/div/p/span", lxml_values(DEEP, "//article/section/div/p/span"))
add(DEEP, "//article//span", lxml_values(DEEP, "//article//span"))

if __name__ == "__main__":
    payload = [[html, xpath, expected] for html, xpath, expected in TRIPLES]
    FIXTURE.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {FIXTURE} ({len(payload)} triples)")
