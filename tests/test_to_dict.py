"""to_dict must be JSON-serializable and must not deep-copy leaf values."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import cast

import pytest

from crawlutron.models.crawl_result import EngineCrawlResult
from crawlutron.models.crawl_strategy_attempt import CrawlStrategyAttempt
from crawlutron.models.discovered_link import DiscoveredLink
from crawlutron.models.serialization import from_wire, to_dict
from crawlutron.vocabulary.strategies import HTTP_DECLARED_CRAWLER
from tests._policy_factories import UNRESTRICTED_HOST_POLICY
from tests._result_builders import _link, engine_result


@dataclass(frozen=True)
class _Link:
    url: str
    host: str


@dataclass(frozen=True)
class _Res:
    html: str
    links: list[_Link] = field(default_factory=list)
    meta: dict[str, object] = field(default_factory=dict)


def test_to_dict_json_safe_and_no_deepcopy():
    html = "x" * 1000
    links = [_Link("http://a", "a"), _Link("http://b", "b")]
    meta: dict[str, str] = {"k": "v"}
    res = _Res(html=html, links=links, meta=cast("dict[str, object]", meta))

    d = to_dict(cast("EngineCrawlResult", res))
    # Must serialize to JSON (nested dataclasses -> dicts).
    json.dumps(d)
    # Heavy string is shared by reference, not copied.
    assert d["html"] is html
    # Nested dataclass converted to a plain dict.
    assert isinstance(d["links"][0], dict)
    assert d["links"][0]["url"] == "http://a"
    # Mutating the returned structure must NOT leak into the source dataclass (containers are rebuilt, leaves
    # shared).
    d["meta"]["k2"] = "w"
    assert "k2" not in res.meta


def test_to_dict_non_dataclass_passthrough():
    assert to_dict(cast("EngineCrawlResult", {"a": 1})) == {"a": 1}
    assert to_dict(cast("EngineCrawlResult", 5)) == 5


def test_to_dict_handles_cyclic_link_graph():
    # A DiscoveredLink-style graph where a node's parent points back to an ancestor must serialize without
    # infinite recursion.
    from dataclasses import dataclass, field

    @dataclass
    class _Node:
        name: str
        parent: _Node | None = None
        children: list[object] = field(default_factory=list)

    a = _Node("a")
    b = _Node("b", parent=a)
    a.children.append(b)
    b.children.append(a)  # cycle: a -> b -> a

    d = to_dict(cast("EngineCrawlResult", a))
    assert isinstance(d, dict)
    # Must terminate and be JSON-serializable.
    json.dumps(d)
    # The cyclic reference is broken (rendered as None) rather than recursing.
    assert d["children"][0]["children"] == [None]


def test_to_dict_breaks_a_cyclic_dict_graph():
    # The dataclass test above breaks its cycle on re-entering a *dataclass*, so the dict and sequence
    # branches each carried their own copy of the cycle guard with nothing pinning them.
    node: dict[str, object] = {"name": "a"}
    node["self"] = node

    d = to_dict(cast("EngineCrawlResult", node))
    assert d["name"] == "a"
    assert d["self"] is None


def test_to_dict_breaks_a_cyclic_sequence_graph():
    items: list[object] = []
    items.append(items)

    d = to_dict(cast("EngineCrawlResult", {"items": items}))
    assert d == {"items": [None]}


# --------------------------------------------------------------------------- from_wire: the derived inverse,
# so a result's key set is spelled ONCE


def test_a_result_survives_the_serialization_round_trip_unchanged():
    """Every field comes back equal. ``from_wire`` inverts ``to_dict`` from the same
    annotations -- one key-set declaration -- and the nested asserts are the load-
    bearing half: a rebuild returning raw dicts would leave consumers off a dict.
    """
    result = engine_result(
        "https://example.test/a",
        "rendered",
        attempted_strategies=[
            CrawlStrategyAttempt(
                strategy=HTTP_DECLARED_CRAWLER,
                status="success",
                classification="rendered",
                http_status=200,
                content_type="text/html",
                elapsed_ms=12,
                error=None,
            )
        ],
        discovered_links=[_link("https://example.test/b", "https://example.test/a")],
        llm_metadata={"title": "T"},
        validators={"etag": "W/1"},
    )

    restored = from_wire(EngineCrawlResult, to_dict(result), policy=UNRESTRICTED_HOST_POLICY)

    assert restored == result
    assert isinstance(restored.attempted_strategies[0], CrawlStrategyAttempt)
    assert isinstance(restored.discovered_links[0], DiscoveredLink)


def test_a_field_the_record_stopped_writing_is_a_construction_error():
    """Drift fails loudly instead of arriving as an absent value: a renamed or dropped
    field used to read as its default and ship missing. A required field absent from
    the payload must raise -- a build-time signal at the spill, not a silent hole.
    """
    payload = to_dict(engine_result("https://example.test/a", "rendered"))
    del payload["original_url"]

    with pytest.raises(TypeError, match="original_url"):
        from_wire(EngineCrawlResult, payload, policy=UNRESTRICTED_HOST_POLICY)


def test_an_absent_field_that_has_a_default_keeps_that_default():
    """Defaults are not a licence to skip required fields: ``to_dict`` always writes
    every field, so an absent one means a producer that is not the serializer -- only
    defaulted fields answer safely (``markdown`` builds, ``original_url`` does not).
    """
    payload = to_dict(engine_result("https://example.test/a", "rendered"))
    del payload["markdown"]

    assert from_wire(EngineCrawlResult, payload, policy=UNRESTRICTED_HOST_POLICY).markdown is None


def test_a_taxonomy_value_outside_the_literals_is_refused():
    """The rebuild keeps the taxonomy check its construction had: ``LiteralChecked``
    runs on ``__post_init__``, so rebuilding validates for free. The pinned case is one
    JSON can produce and a dataclass cannot -- a status string no producer emits.
    """
    payload = to_dict(engine_result("https://example.test/a", "rendered"))
    payload["status"] = "not-a-real-status"

    with pytest.raises(ValueError):
        from_wire(EngineCrawlResult, payload, policy=UNRESTRICTED_HOST_POLICY)
