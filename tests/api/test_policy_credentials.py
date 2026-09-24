"""Tier 2b: policy.credentials -- scoped, typed, host-bounded credentials.

M12 banned spoofable headers but deliberately kept ``authorization``
available: credentialed crawling of one's own site is real use, and banning
the header would push callers to bake credentials into URLs (userinfo),
which then leak through logs, Referer and retries. The typed field is the
replacement path: credentials are keyed by canonical origin, so the
existing cross-host redirect scrub (Fix 3, ``_CREDENTIAL_HEADERS``) is also
the enforcement point for "credentials never cross host boundaries" -- a
credential attached to origin A is scrubbed from every request to origin B
by the chokepoint that already scrubs cookies.
"""

import pytest

from app.api_models import CrawlPolicy, Credential
from app.crawl.payload_helpers import _payload_policy
from app.crawl.strategies.request_profile import _apply_credentials
from app.settings import CrawlerSettings
from app.validation import HostPolicy


def _settings() -> CrawlerSettings:
    return CrawlerSettings.from_mapping({})


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
        return {
            "https://private.example": {"kind": "bearer", "token": "tok"},
            "http://legacy.example:8080": {"kind": "basic", "username": "u", "password": "p"},
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
        """The invariant rides Fix 3: authorization is in _CREDENTIAL_HEADERS,
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
        assert result is not None
        _, forwarded = result
        assert "Authorization" not in forwarded

    def test_headers_for_strategy_attaches_only_the_matching_origin(self):
        """Integration through the real header assembly seam (the only
        consumer): seed URL gets the header, any other origin does not."""
        from app.crawl.strategies.request_profile import _headers_for_strategy

        policy = {
            "headers": {},
            "request_profile": {},
            "credentials": self._credentials(),
        }
        scoped = _headers_for_strategy("http_declared_crawler", policy, "https://private.example/docs")
        assert scoped["Authorization"] == "Bearer tok"
        plain = _headers_for_strategy("http_declared_crawler", policy, "https://public.example/docs")
        assert "Authorization" not in plain



class TestPayloadPolicy:
    def test_keys_canonicalize_to_origin_form(self):
        policy = _payload_policy(
            {"policy": {"credentials": {"HTTPS://Private.Example:443/": {"kind": "bearer", "token": "t"}}}},
            _settings(),
        )
        assert policy["credentials"] == {"https://private.example": {"kind": "bearer", "token": "t"}}

    def test_malformed_credential_is_rejected_not_dropped(self):
        with pytest.raises(ValueError, match="credentials"):
            _payload_policy({"policy": {"credentials": {"https://a.example": {"kind": "carrier"}}}}, _settings())
