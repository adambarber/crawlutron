"""json_safe chokepoint contract: one canonical rendering per type."""

from __future__ import annotations

import datetime
import json

from app.json_safe import json_default


class TestJsonDefault:
    def test_pydantic_reducers_first(self):
        class M:
            def model_dump(self):
                return {"a": 1}

        assert json.dumps(M(), default=json_default) == '{"a": 1}'

    def test_datetime_isoformat(self):
        assert json.dumps(datetime.datetime(2024, 1, 2, 3, 4, 5), default=json_default) == '"2024-01-02T03:04:05"'
        assert json.dumps(datetime.date(2024, 1, 2), default=json_default) == '"2024-01-02"'

    def test_bytes_decoded(self):
        assert json.dumps(b"ab\xff", default=json_default) == '"ab\\ufffd"'

    def test_set_becomes_list(self):
        assert json.loads(json.dumps({1}, default=json_default)) == [1]

    def test_exception_carries_class_name(self):
        assert json.dumps(ValueError("bad"), default=json_default) == '"ValueError: bad"'

    def test_plain_object_uses_dict(self):
        class P:
            def __init__(self):
                self.x = 1
                self._hidden = 2

        assert json.dumps(P(), default=json_default) == '{"x": 1}'

    def test_fallback_is_str(self):
        assert json.dumps(object(), default=json_default).startswith('"')
