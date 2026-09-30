"""to_dict must be JSON-serializable and must not deep-copy leaf values."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from app.models import to_dict


@dataclass(frozen=True)
class _Link:
    url: str
    host: str


@dataclass(frozen=True)
class _Res:
    html: str
    links: list[_Link] = field(default_factory=list)
    meta: dict = field(default_factory=dict)


def test_to_dict_json_safe_and_no_deepcopy():
    html = "x" * 1000
    links = [_Link("http://a", "a"), _Link("http://b", "b")]
    meta = {"k": "v"}
    res = _Res(html=html, links=links, meta=meta)

    d = to_dict(res)
    # Must serialize to JSON (nested dataclasses -> dicts).
    json.dumps(d)
    # Heavy string is shared by reference, not copied.
    assert d["html"] is html
    # Nested dataclass converted to a plain dict.
    assert isinstance(d["links"][0], dict)
    assert d["links"][0]["url"] == "http://a"
    # Mutating the returned structure must NOT leak into the source dataclass
    # (containers are rebuilt, leaves shared).
    d["meta"]["k2"] = "w"
    assert "k2" not in res.meta


def test_to_dict_non_dataclass_passthrough():
    assert to_dict({"a": 1}) == {"a": 1}
    assert to_dict(5) == 5


def test_to_dict_handles_cyclic_link_graph():
    # A DiscoveredLink-style graph where a node's parent points back to an
    # ancestor must serialize without infinite recursion.
    from dataclasses import dataclass, field

    @dataclass
    class _Node:
        name: str
        parent: _Node | None = None
        children: list = field(default_factory=list)

    a = _Node("a")
    b = _Node("b", parent=a)
    a.children.append(b)
    b.children.append(a)  # cycle: a -> b -> a

    d = to_dict(a)
    assert isinstance(d, dict)
    # Must terminate and be JSON-serializable.
    json.dumps(d)
    # The cyclic reference is broken (rendered as None) rather than recursing.
    assert d["children"][0]["children"] == [None]


def test_to_dict_breaks_a_cyclic_dict_graph():
    # The dataclass test above breaks its cycle on re-entering a *dataclass*, so
    # the dict and sequence branches each carried their own copy of the cycle
    # guard with nothing pinning them. A self-referential mapping is the shape
    # that exercises the dict branch's guard on its own.
    node: dict = {"name": "a"}
    node["self"] = node

    d = to_dict(node)
    assert d["name"] == "a"
    assert d["self"] is None


def test_to_dict_breaks_a_cyclic_sequence_graph():
    items: list = []
    items.append(items)

    d = to_dict({"items": items})
    assert d == {"items": [None]}
