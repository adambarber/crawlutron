"""H2 parse-budget contract: lxml tree parses per page, measured not assumed.

The chokepoint-theory review claimed "two parses per page"; measurement
through the real production paths (this file) found the codebase's own
accounting in response_shaping's PERF TODO to be exact: trafilatura (its own
parser, not counted here), ONE shared lxml tree in the engine's discovery
block (tree= threading covers diagnostics/frames/forms/links), and ONE
re-parse in _extract_llm_metadata at shaping time, across the persistence
boundary. These tests pin the lxml counts as CEILINGS; a fifth call site or a
duplicated parse inside a stage fails here rather than showing up in a perf
profile.

The escalation decision these tests instrument: consolidation (reading title/
meta from the discovery tree and threading it to shaping) is Tier-2 scoped
because the engine result is persisted and shaped from plain dicts -- the
tree must not escape, so consolidation means carrying metadata, not the tree.
"""

import pytest
from _testkit import make_fetch

from app.crawl import extraction
from app.crawl.strategies.http import _http_strategy_result

HTML = (
    "<html><head><title>T</title></head><body>"
    '<a href="/next">n</a><form action="/p"><input name="q"></form><p>hello</p>'
    "</body></html>"
)


@pytest.fixture
def parse_counter(monkeypatch):
    calls: list[str] = []
    real = extraction._parse_html_tree

    def counting(html):
        calls.append(html if isinstance(html, str) else repr(html))
        return real(html)

    monkeypatch.setattr(extraction, "_parse_html_tree", counting)
    return calls


async def _run(policy_extra: dict) -> None:
    async def fetcher(url, headers, timeout, max_content_length=50 * 1024 * 1024):
        return make_fetch(
            url=url,
            final_url=url,
            status=200,
            headers={"content-type": "text/html"},
            body=HTML.encode(),
        )

    await _http_strategy_result(
        original_url="http://example.com/",
        normalized_url="http://example.com/",
        strategy="HTTP_DECLARED_CRAWLER",
        policy={"strategies": ["HTTP_DECLARED_CRAWLER"], "limits": {}, "headers": {}, **policy_extra},
        http_fetcher=fetcher,
        caller_validators=None,
    )


async def test_single_shot_parses_no_tree(parse_counter):
    """No discovery, no links, no selector: zero lxml parses in the engine."""
    await _run({})
    assert parse_counter == []


async def test_link_discovery_is_one_shared_parse(parse_counter):
    """The frontier path parses ONCE; frames/forms/diagnostics reuse the tree."""
    await _run({"limits": {"max_depth": 1}})
    assert len(parse_counter) == 1


async def test_selector_mode_adds_no_parse_inside_the_strategy(parse_counter):
    """Selector + discovery is still ONE parse inside the strategy layer.

    Selector extraction itself parses later (engine phase, extract_content ->
    _extract_selected), so a selector-mode page totals two lxml parses across
    the pipeline -- but the strategy stage keeps its one-shared-tree
    guarantee. Ceiling, not target: consolidation may drop the total, never
    raise the per-stage count.
    """
    await _run({"css_selector": "body", "limits": {"max_depth": 1}})
    assert len(parse_counter) == 1


async def test_shaping_reparses_exactly_once_per_rendered_item(parse_counter):
    """The cross-boundary re-parse is one per item, not one per field.

    _extract_llm_metadata runs after persistence, reading the stored html
    string -- it cannot see the engine tree. This pins that the shaping side
    adds exactly one parse; consolidation would take it to zero by carrying
    precomputed metadata, which is Tier-2 territory.
    """
    from app.api.response_shaping import format_crawl_item

    item = {
        "final_url": "http://example.com/",
        "markdown": "# hello",
        "html": HTML,
    }
    parse_counter.clear()
    _text, metadata = format_crawl_item(item)
    assert metadata  # metadata was actually extracted, so the parse happened
    assert len(parse_counter) == 1
