"""``authorization`` stays while other spoofable headers are banned, because
credentialed crawling of what one owns is real use and a ban would push
secrets into URLs; origin-keyed fields ride the existing cross-host scrub.
"""

import pytest

from app.models.credential import Credential
from app.transport.request_profile import apply_credentials
from tests._policy_factories import CrawlPolicy, PolicyTier, canonical, payload_policy, read_policy

#: The settings a payload-policy test runs against: the defaults with nothing overridden.
MODEL_DEFAULTS_TIER = PolicyTier.model_defaults()


class TestWireModel:
    def test_bearer_requires_a_token(self):
        with pytest.raises(ValueError):
            Credential(kind="bearer")

    def test_basic_requires_username_and_password(self):
        with pytest.raises(ValueError):
            Credential(kind="basic", username="u")

    def test_policy_accepts_origin_keyed_credentials(self):
        policy = CrawlPolicy(credentials={"https://private.example": {"kind": "bearer", "token": "t"}})
        assert policy.credentials["https://private.example"].token == "t"

    def test_policy_rejects_non_origin_keys(self):
        # A path/query key can never match a per-hop origin lookup: reject, never silently normalize a
        # caller's intent into a different scope.
        with pytest.raises(ValueError):
            CrawlPolicy(credentials={"https://private.example/secret": {"kind": "bearer", "token": "t"}})


class TestApplyCredentials:
    def _credentials(self):
        return {
            "https://private.example": Credential(kind="bearer", token="tok"),
            "http://legacy.example:8080": Credential(kind="basic", username="u", password="p"),
        }

    def test_matching_origin_gets_authorization(self):
        import base64

        headers = apply_credentials({}, canonical("https://private.example/page"), self._credentials())
        assert headers["Authorization"] == "Bearer tok"
        # Basic credentials canonicalize into the standard header value.
        headers = apply_credentials({}, canonical("http://legacy.example:8080/x"), self._credentials())
        expected = base64.b64encode(b"u:p").decode()
        assert headers["Authorization"] == f"Basic {expected}"

    def test_other_origins_get_nothing(self):
        headers = apply_credentials(
            {"X": "1"}, canonical("https://elsewhere.example/page"), self._credentials()
        )
        assert headers == {"X": "1"}
        # Default-port and case variants of the REQUEST url match the same origin.
        headers = apply_credentials({}, canonical("https://PRIVATE.example:443/page"), self._credentials())
        assert headers["Authorization"] == "Bearer tok"

    def test_headers_for_strategy_attaches_only_the_matching_origin(self):
        """Integration through the real header assembly seam (the only
        consumer): seed URL gets the header, any other origin does not."""
        from app.transport.request_profile import headers_for_strategy

        policy = {
            "headers": {},
            "request_profile": {},
            "credentials": self._credentials(),
        }
        scoped = headers_for_strategy(
            "http_declared_crawler", read_policy(policy), canonical("https://private.example/docs")
        )
        assert scoped["Authorization"] == "Bearer tok"
        plain = headers_for_strategy(
            "http_declared_crawler", read_policy(policy), canonical("https://public.example/docs")
        )
        assert "Authorization" not in plain


class TestPayloadPolicy:
    def test_keys_canonicalize_to_origin_form(self):
        policy = payload_policy(
            {"policy": {"credentials": {"HTTPS://Private.Example:443/": {"kind": "bearer", "token": "t"}}}},
            MODEL_DEFAULTS_TIER,
        )
        assert list(policy.credentials) == ["https://private.example"]
        assert policy.credentials["https://private.example"].token == "t"

    def test_malformed_credential_is_rejected_not_dropped(self):
        with pytest.raises(ValueError, match="credentials"):
            payload_policy(
                {"policy": {"credentials": {"https://a.example": {"kind": "carrier"}}}}, MODEL_DEFAULTS_TIER
            )

    def test_two_keys_collapsing_to_one_origin_are_rejected(self):
        """Two spellings of one origin must not silently become one credential: the
        canonicalizer keeps the last, so the caller lists two and has one -- the
        same collapse its sibling validator rejects by refusing a path-scoped key.
        """
        with pytest.raises(ValueError) as exc:
            CrawlPolicy(
                credentials={
                    "HTTPS://a.example": {"kind": "bearer", "token": "first"},
                    "https://a.example:443": {"kind": "bearer", "token": "second"},
                }
            )
        message = str(exc.value)
        assert "HTTPS://a.example" in message, message
        assert "https://a.example:443" in message, message

    def test_a_colliding_credential_map_is_rejected_on_the_wire_path(self):
        """The wire path must reject too, not repair the collision away:
        ``read_policy`` repairs field by field, so a fixable collision would drop
        one credential and validate -- the original bug with an extra step.
        """
        with pytest.raises(ValueError, match="invalid_policy_credentials"):
            payload_policy(
                {
                    "policy": {
                        "credentials": {
                            "HTTPS://a.example": {"kind": "bearer", "token": "first"},
                            "https://a.example:443": {"kind": "bearer", "token": "second"},
                        }
                    }
                },
                MODEL_DEFAULTS_TIER,
            )

    def test_distinct_origins_still_validate(self):
        """The collision check must not reject a policy that merely looks similar."""
        policy = payload_policy(
            {
                "policy": {
                    "credentials": {
                        "https://a.example": {"kind": "bearer", "token": "one"},
                        "https://b.example": {"kind": "bearer", "token": "two"},
                        "https://a.example:8443": {"kind": "bearer", "token": "three"},
                    }
                }
            },
            MODEL_DEFAULTS_TIER,
        )
        assert set(policy.credentials) == {
            "https://a.example",
            "https://b.example",
            "https://a.example:8443",
        }

    def test_an_already_canonical_policy_revalidates_without_collision(self):
        """Re-validating a validated policy is how one round-trips through storage; a
        check comparing keys rather than canonical forms would report an already-
        canonical policy as colliding with itself on every reload.
        """
        once = payload_policy(
            {"policy": {"credentials": {"https://a.example": {"kind": "bearer", "token": "t"}}}},
            MODEL_DEFAULTS_TIER,
        )
        twice = payload_policy({"policy": {"credentials": dict(once.credentials)}}, MODEL_DEFAULTS_TIER)
        assert dict(twice.credentials) == dict(once.credentials)
