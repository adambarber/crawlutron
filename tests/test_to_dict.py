"""#2: to_dict must be JSON-serializable and must not deep-copy leaf values."""

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
