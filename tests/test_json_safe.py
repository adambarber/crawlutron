"""json_safe chokepoint contract: one canonical rendering per type."""

from __future__ import annotations

import datetime
import json

import pytest

from crawlutron.admission.credentials import origin_of
from crawlutron.models.crawl_policy import CrawlPolicy
from crawlutron.models.credential import Credential
from crawlutron.safety.json_safe import (
    MAX_JSON_INPUT_BYTES,
    ParseRefused,
    json_default,
    parse_json,
)


class TestJsonDefault:
    def test_pydantic_reducers_first(self):
        class M:
            def model_dump(self):
                return {"a": 1}

        assert json.dumps(M(), default=json_default) == '{"a": 1}'

    def test_datetime_isoformat(self):
        assert (
            json.dumps(datetime.datetime(2024, 1, 2, 3, 4, 5), default=json_default)
            == '"2024-01-02T03:04:05"'
        )
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

    def test_secretstr_redacts_never_unseals(self):
        secret = Credential(kind="bearer", token="supersecret123")

        rendered = json.dumps(secret, default=json_default)

        assert "supersecret123" not in rendered
        assert "REDACTED" in rendered

    def test_stored_policy_never_carries_a_plaintext_credential(self):
        """The write edge persists policy rows; the serializer must never be a
        secret's exit, so the stored row redacts the credential value while the
        rest of the policy round-trips."""
        origin = origin_of("https://example.test")
        policy = CrawlPolicy.model_validate(
            {
                "strategies": ["BROWSER_HEADLESS"],
                "limits": {"max_depth": 2},
                "credentials": {origin: {"kind": "bearer", "token": "supersecret123"}},
            }
        )

        rendered = json.dumps(policy.model_dump(), default=json_default)

        assert "supersecret123" not in rendered
        assert json.loads(rendered)["strategies"] == ["BROWSER_HEADLESS"]
        assert json.loads(rendered)["limits"]["max_depth"] == 2
        credential = json.loads(rendered)["credentials"][origin]
        assert credential["kind"] == "bearer"
        assert "REDACTED" in credential["token"]


class TestParseJson:
    """parse_json is the admit-or-refuse door that json_default's serializer
    never had: cap bytes, bound depth, enforce the admitted shape, and refuse
    with a message that never echoes the payload.
    """

    def test_valid_object_within_depth_is_admitted(self):
        assert parse_json('{"a": 1, "b": {"c": 2}}', expected=dict, max_depth=3) == {
            "a": 1,
            "b": {"c": 2},
        }

    def test_valid_list_within_depth_is_admitted(self):
        assert parse_json("[1, [2, 3]]", expected=list, max_depth=3) == [1, [2, 3]]

    def test_depth_over_bound_is_refused_by_name(self):
        # top object = 1, "a"'s object = 2, "b"'s object = 3; bound is 2
        with pytest.raises(ParseRefused):
            parse_json('{"a": {"b": {}}}', expected=dict, max_depth=2)

    def test_depth_at_bound_is_admitted(self):
        # top object = 1, nested object = 2; bound is 2
        assert parse_json('{"a": {}}', expected=dict, max_depth=2) == {"a": {}}

    def test_non_object_when_object_expected_is_refused_by_name(self):
        with pytest.raises(ParseRefused):
            parse_json("[1, 2, 3]", expected=dict, max_depth=5)

    def test_invalid_json_is_refused_by_name(self):
        with pytest.raises(ParseRefused):
            parse_json("{not json", expected=dict, max_depth=5)

    def test_oversized_payload_is_refused_by_name(self):
        payload = "x" * (MAX_JSON_INPUT_BYTES + 1)
        with pytest.raises(ParseRefused):
            parse_json(payload, expected=dict, max_depth=5)

    def test_refused_message_never_echoes_the_payload(self):
        secret = '{"token": "supersecret123"}'
        try:
            parse_json(secret * 100000, expected=dict, max_depth=5)
        except ParseRefused as error:
            assert "supersecret123" not in str(error)
        else:
            raise AssertionError("expected ParseRefused for an oversized payload")

    def test_parse_json_bytes_input_is_refused(self):
        with pytest.raises(ParseRefused):
            parse_json(b'{"a": 1}', expected=dict, max_depth=5)

    def test_parse_json_empty_string_is_refused(self):
        with pytest.raises(ParseRefused):
            parse_json("", expected=dict, max_depth=5)

    def test_parse_json_null_scalar_is_refused(self):
        with pytest.raises(ParseRefused):
            parse_json("null", expected=dict, max_depth=5)

    def test_parse_json_max_depth_zero_refuses_everything(self):
        with pytest.raises(ParseRefused):
            parse_json("{}", expected=dict, max_depth=0)
        with pytest.raises(ParseRefused):
            parse_json("[]", expected=list, max_depth=0)

    def test_parse_json_max_depth_one_admits_flat_refuses_nested(self):
        assert parse_json("{}", expected=dict, max_depth=1) == {}
        assert parse_json("[]", expected=list, max_depth=1) == []
        assert parse_json('{"a": 1}', expected=dict, max_depth=1) == {"a": 1}
        with pytest.raises(ParseRefused):
            parse_json('{"a": {}}', expected=dict, max_depth=1)
        with pytest.raises(ParseRefused):
            parse_json('{"a": {"b": {}}}', expected=dict, max_depth=1)
        with pytest.raises(ParseRefused):
            parse_json("[[[]]]", expected=list, max_depth=1)

    def test_parse_json_trailing_garbage_is_refused(self):
        with pytest.raises(ParseRefused):
            parse_json("{} junk", expected=dict, max_depth=5)

    def test_parse_json_unicode_escape_parses(self):
        assert parse_json(r'{"a":"\u0022"}', expected=dict, max_depth=5) == {"a": '"'}
