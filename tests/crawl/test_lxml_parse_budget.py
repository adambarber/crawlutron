"""Lxml tree parses per page, measured not assumed.

The chokepoint-theory review claimed "two parses per page"; measurement
through the real production paths (this file) found the codebase's own
accounting in response_shaping's PERF TODO to be exact: trafilatura (its own
parser, not counted here), ONE shared lxml tree in the engine's discovery
block (tree= threading covers diagnostics/frames/forms/links), and ONE
re-parse in _extract_llm_metadata at shaping time, across the persistence
boundary.

Consolidation moved the shaping-side re-parse to its computation site:
when the discovery tree exists it also reads the LLM page metadata once, and
the result carries the small plain-data dict (``llm_metadata``) across the
persistence boundary; shaping reads the carried dict instead of rebuilding a
tree. The shaping ceiling is therefore 0 for every item the engine produced
from a parsed body; an engine item with no discovery tree (single-shot,
no links/diagnostics requested) never parses at engine stage and shaping
still reparses the ONE time (total unchanged). The tree itself still never
escapes the worker thread: what crosses the boundary is a JSON dict.

These tests pin the lxml counts as CEILINGS; a new call site or a
duplicated parse inside a stage fails here rather than in a perf profile.
"""

import pytest

from app.crawl import extraction
from tests._fetcher_factories import run_http_strategy, scripted_fetcher

HTML = (
    "<html><head><title>T</title></head><body>"
    '<a href="/next">n</a><form action="/p"><input name="q"></form><p>hello</p>'
    "</body></html>"
)

META_HTML = (
    '<html lang="en"><head><title>Meta T</title>'
    '<meta name="description" content="the description">'
    '<meta property="og:image" content="https://cdn.example/i.png">'
    '<link rel="canonical" href="https://example.com/canon">'
    "</head><body><p>content with enough words for trafilatura to build a "
    "markdown body out of this page, because the extraction pipeline runs "
    "before shaping and the carried metadata must survive it.</p></body></html>"
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


async def _run(policy_extra: dict, *, html: str = HTML):
    fetcher = scripted_fetcher(status=200, headers={"content-type": "text/html"}, body=html.encode())

    return await run_http_strategy(
        url="http://example.com/",
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
    extract_selected), so a selector-mode page totals two lxml parses across
    the pipeline -- but the strategy stage keeps its one-shared-tree
    guarantee. Ceiling, not target: consolidation may drop the total, never
    raise the per-stage count.
    """
    await _run({"css_selector": "body", "limits": {"max_depth": 1}})
    assert len(parse_counter) == 1


async def test_engine_computes_metadata_once_from_the_discovery_tree(parse_counter):
    """The discovery tree also produces the page metadata, once.

    The engine item carries the small metadata dict (persisted, plain data)
    so shaping never rebuilds a tree from the stored HTML string.
    """
    result = await _run({"limits": {"max_depth": 1}}, html=META_HTML)
    # Engine stage still owns exactly one parse; the metadata read added none.
    assert len(parse_counter) == 1
    assert result.llm_metadata
    assert result.llm_metadata["title"] == "Meta T"
    assert result.llm_metadata["description"] == "the description"


async def test_shaping_never_reparses_engine_produced_items(parse_counter):
    """The shaping ceiling for engine items went 1 -> 0: it reads the dict."""
    import json

    from app.api.response_shaping import format_crawl_item
    from app.crawl.llm_format import _extract_llm_metadata
    from app.models import to_dict

    result = await _run({"limits": {"max_depth": 1}}, html=META_HTML)
    item = to_dict(result) | {"markdown": result.markdown or "# hello"}
    # The carried payload crosses the persistence boundary as plain JSON.
    json.dumps(item)
    parse_counter.clear()
    _markdown, metadata = format_crawl_item(item)
    assert parse_counter == [], "shaping rebuilt an lxml tree from the carried item"
    # Byte-for-byte the same computation site output the metadata replaced.
    assert metadata == _extract_llm_metadata(META_HTML, "http://example.com/")


async def test_engine_item_without_a_tree_still_shapes_once(parse_counter):
    """Single-shot pages never parse at engine stage; shaping keeps their one.

    Carrying is conditional on a tree the pipeline built anyway -- asking
    the metadata to force its own parse would REGRESS the single-shot
    budget (test_single_shot_parses_no_tree), so the fallback stays.
    """
    from app.api.response_shaping import format_crawl_item
    from app.models import to_dict

    result = await _run({}, html=META_HTML)
    assert result.llm_metadata is None  # no tree existed to read from
    item = to_dict(result) | {"markdown": "# hello"}
    parse_counter.clear()
    _markdown, metadata = format_crawl_item(item)
    assert len(parse_counter) == 1
    assert metadata["title"] == "Meta T"


async def test_carried_metadata_never_reaches_the_wire():
    """The wire shape is unchanged: llm_metadata is an internal field."""
    from app.api.response_shaping import build_unified_result
    from app.models import to_dict

    result = await _run({"limits": {"max_depth": 1}}, html=META_HTML)
    unified = build_unified_result(to_dict(result))
    dumped = unified.model_dump()
    assert "llm_metadata" not in dumped
    # ...and its metadata is the SAME wire surface the old reparse produced.
    assert dumped["metadata"]["title"] == "Meta T"
    assert dumped["metadata"]["source_url"] == "http://example.com/"
