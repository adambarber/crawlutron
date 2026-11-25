"""Bounded JSON admit/refuse door for crawlutron_extract: bytes refused, byte cap,
depth cap, malformed refused, root-shape enforced, refusal message never echoes
the attacker-controlled payload.
"""

from __future__ import annotations

import pytest

from crawlutron_extract.json_door import (
    MAX_JSON_DEPTH,
    MAX_JSON_INPUT_BYTES,
    JsonRefused,
    parse_json,
)


def test_valid_object_admitted():
    assert parse_json('{"a": 1, "b": [2]}') == {"a": 1, "b": [2]}


def test_valid_list_admitted():
    assert parse_json("[1, 2, 3]") == [1, 2, 3]


def test_bytes_input_refused():
    with pytest.raises(JsonRefused):
        parse_json(b'{"a": 1}')


def test_empty_string_refused():
    with pytest.raises(JsonRefused):
        parse_json("")


def test_null_scalar_refused_when_object_expected():
    with pytest.raises(JsonRefused):
        parse_json("null", expected=(dict, list))


def test_numeric_scalar_refused_when_object_expected():
    with pytest.raises(JsonRefused):
        parse_json("42", expected=(dict, list))


def test_string_scalar_refused_when_object_expected():
    with pytest.raises(JsonRefused):
        parse_json('"hello"', expected=(dict, list))


def test_trailing_garbage_refused():
    with pytest.raises(JsonRefused):
        parse_json("{} junk")


def test_unicode_escape_admitted():
    assert parse_json(r'{"a":"\u0022"}') == {"a": '"'}


def test_max_depth_zero_refuses_root_and_nested():
    # root container counts as depth 1 (core convention): {} and [] refused at
    # max_depth=0.
    with pytest.raises(JsonRefused):
        parse_json("{}", max_depth=0)
    with pytest.raises(JsonRefused):
        parse_json("[]", max_depth=0)
    with pytest.raises(JsonRefused):
        parse_json('{"a": {}}', max_depth=0)
    with pytest.raises(JsonRefused):
        parse_json("[[]]", max_depth=0)


def test_max_depth_one_admits_flat_refuses_nested():
    assert parse_json("{}", max_depth=1) == {}
    assert parse_json("[]", max_depth=1) == []
    assert parse_json('{"a": 1}', max_depth=1) == {"a": 1}
    # {"a": {}} is depth 2: refused at max_depth=1.
    with pytest.raises(JsonRefused):
        parse_json('{"a": {}}', max_depth=1)
    with pytest.raises(JsonRefused):
        parse_json("[[]]", max_depth=1)
    with pytest.raises(JsonRefused):
        parse_json('{"a": {"b": {}}}', max_depth=1)
    with pytest.raises(JsonRefused):
        parse_json("[[[]]]", max_depth=1)


def test_depth_over_max_refused():
    payload = "[" * (MAX_JSON_DEPTH + 1) + "0" + "]" * (MAX_JSON_DEPTH + 1)
    with pytest.raises(JsonRefused):
        parse_json(payload, max_depth=MAX_JSON_DEPTH)


def test_depth_at_max_admitted():
    payload = "[" * MAX_JSON_DEPTH + "0" + "]" * MAX_JSON_DEPTH
    # depth equals MAX_JSON_DEPTH -> admitted (bound is inclusive)
    parse_json(payload, max_depth=MAX_JSON_DEPTH)


def test_oversized_payload_refused():
    payload = " " * (MAX_JSON_INPUT_BYTES + 1)
    with pytest.raises(JsonRefused):
        parse_json(payload)


def test_refused_message_never_echoes_payload():
    secret = '{"token": "supersecret123"}'
    try:
        parse_json(secret * 100000)
    except JsonRefused as error:
        assert "supersecret123" not in str(error)
    else:
        raise AssertionError("expected JsonRefused for an oversized payload")


def test_malformed_json_refused():
    with pytest.raises(JsonRefused):
        parse_json("{not json")


def test_expected_root_shape_enforced():
    # default admits any JSON root
    assert parse_json("[1, 2, 3]") == [1, 2, 3]
    # expected=list admits; expected=dict refuses
    assert parse_json("[1, 2, 3]", expected=list) == [1, 2, 3]
    with pytest.raises(JsonRefused):
        parse_json("[1, 2, 3]", expected=dict)
    assert parse_json('{"a": 1}', expected=dict) == {"a": 1}
    with pytest.raises(JsonRefused):
        parse_json('{"a": 1}', expected=list)
