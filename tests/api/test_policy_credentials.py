"""policy.credentials: scoped, typed, host-bounded credentials.

Spoofable headers are banned but deliberately kept ``authorization``
available: credentialed crawling of one's own site is real use, and banning
the header would push callers to bake credentials into URLs (userinfo),
which then leak through logs, Referer and retries. The typed field is the
replacement path: credentials are keyed by canonical origin, so the
existing cross-host redirect scrub (``_CREDENTIAL_HEADERS``) is also
the enforcement point for "credentials never cross host boundaries" -- a
credential attached to origin A is scrubbed from every request to origin B
by the chokepoint that already scrubs cookies.
"""

import pytest

from app.crawl.payload_helpers import _payload_policy
from app.crawl.policy import PolicyTier, read_policy
from app.crawl.strategies.request_profile import _apply_credentials
from app.models.crawl_policy import CrawlPolicy
from app.models.credential import Credential
from app.validation import HostPolicy

#: The settings a payload-policy test runs against: the defaults with nothing
#: overridden. A module constant rather than a factory because it is immutable
#: and identical at every call site -- an environment default leaking in here
#: would be a second variable in a test about credential keying.
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
        # A path/query key can never match a per-hop origin lookup: reject,
        # never silently normalize a caller's intent into a different scope.
        with pytest.raises(ValueError):
            CrawlPolicy(credentials={"https://private.example/secret": {"kind": "bearer", "token": "t"}})


class TestApplyCredentials:
    def _credentials(self):
        # Credential models, not raw mappings: production hands
        # apply_credentials the validated model, and a test that passed
        # mappings was exercising a shape the header path no longer has.
        return {
            "https://private.example": Credential(kind="bearer", token="tok"),
            "http://legacy.example:8080": Credential(kind="basic", username="u", password="p"),
        }

    def test_matching_origin_gets_authorization(self):
        import base64

        headers = _apply_credentials({}, "https://private.example/page", self._credentials())
        assert headers["Authorization"] == "Bearer tok"
        # Basic credentials canonicalize into the standard header value.
        headers = _apply_credentials({}, "http://legacy.example:8080/x", self._credentials())
        expected = base64.b64encode(b"u:p").decode()
        assert headers["Authorization"] == f"Basic {expected}"

    def test_other_origins_get_nothing(self):
        headers = _apply_credentials({"X": "1"}, "https://elsewhere.example/page", self._credentials())
        assert headers == {"X": "1"}
        # Default-port and case variants of the REQUEST url match the same origin.
        headers = _apply_credentials({}, "https://PRIVATE.example:443/page", self._credentials())
        assert headers["Authorization"] == "Bearer tok"

    def test_scrubbed_on_cross_host_redirect_by_the_existing_chokepoint(self):
        """The invariant: authorization is in _CREDENTIAL_HEADERS,
        so the manual redirect loop strips it when the hop changes host --
        credentials applied to the seed URL never reach the redirect target."""
        from app.crawl.strategies.pinned_transport import _CREDENTIAL_HEADERS, _redirect_target

        assert "authorization" in _CREDENTIAL_HEADERS
        headers = {"Authorization": "Bearer tok"}
        result = _redirect_target(
            "https://private.example/a",
            headers,
            302,
            "https://other.test/a",
            0,
            policy=HostPolicy.unrestricted(),
        )
        assert result == ("https://other.test/a", {})
        _, forwarded = result
        assert "Authorization" not in forwarded

    def test_headers_for_strategy_attaches_only_the_matching_origin(self):
        """Integration through the real header assembly seam (the only
        consumer): seed URL gets the header, any other origin does not."""
        from app.crawl.strategies.request_profile import headers_for_strategy

        policy = {
            "headers": {},
            "request_profile": {},
            "credentials": self._credentials(),
        }
        scoped = headers_for_strategy(
            "http_declared_crawler", read_policy(policy), "https://private.example/docs"
        )
        assert scoped["Authorization"] == "Bearer tok"
        plain = headers_for_strategy(
            "http_declared_crawler", read_policy(policy), "https://public.example/docs"
        )
        assert "Authorization" not in plain


class TestPayloadPolicy:
    def test_keys_canonicalize_to_origin_form(self):
        policy = _payload_policy(
            {"policy": {"credentials": {"HTTPS://Private.Example:443/": {"kind": "bearer", "token": "t"}}}},
            MODEL_DEFAULTS_TIER,
        )
        assert list(policy.credentials) == ["https://private.example"]
        assert policy.credentials["https://private.example"].token == "t"

    def test_malformed_credential_is_rejected_not_dropped(self):
        with pytest.raises(ValueError, match="credentials"):
            _payload_policy(
                {"policy": {"credentials": {"https://a.example": {"kind": "carrier"}}}}, MODEL_DEFAULTS_TIER
            )

    def test_two_keys_collapsing_to_one_origin_are_rejected(self):
        """Two spellings of one origin must not silently become one credential.

        Both keys canonicalize to ``https://a.example``, so the comprehension that
        builds the canonical map keeps the last and drops the first. The caller
        then holds a policy that lists two credentials and has one, and nothing
        at either end says so -- the same failure the sibling validator exists to
        prevent when it REJECTS a path-scoped key, since that path would silently
        widen a credential rather than silently narrow the set.

        Asserted at the MODEL boundary because that is where the detail lives.
        Over HTTP FastAPI validates ``CrawlRequest.policy`` as this model before
        the handler runs, so this message is what a caller is shown. The bare-code
        edges (MCP, direct caller) keep the code, exactly as they do for every
        other validation error here -- so the code is asserted separately below
        rather than pretending the detail rides along on edges that drop it.
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
        """The wire path must reject too, not repair the collision away.

        ``read_policy`` is total and repairs a policy field by field. A collision
        is not a bad single field -- it is a disagreement BETWEEN two fields -- so
        if this ever became repairable, the repair would drop one of the two
        credentials and return a policy that validated. That is the original bug
        with an extra step, so the wire path is asserted to reject outright.
        """
        with pytest.raises(ValueError, match="invalid_policy_credentials"):
            _payload_policy(
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
        policy = _payload_policy(
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
        """Re-validating a validated policy is how a policy round-trips through storage.

        If the collision check compared keys against each other rather than
        canonical forms, a policy that was already canonical would report itself
        as colliding with itself on every reload.
        """
        once = _payload_policy(
            {"policy": {"credentials": {"https://a.example": {"kind": "bearer", "token": "t"}}}},
            MODEL_DEFAULTS_TIER,
        )
        twice = _payload_policy({"policy": {"credentials": dict(once.credentials)}}, MODEL_DEFAULTS_TIER)
        assert dict(twice.credentials) == dict(once.credentials)
