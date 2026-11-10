"""json_safe chokepoint contract: one canonical rendering per type."""

from __future__ import annotations

import datetime
import json

from crawlutron.admission.credentials import origin_of
from crawlutron.models.crawl_policy import CrawlPolicy
from crawlutron.models.credential import Credential
from crawlutron.safety.json_safe import json_default


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
