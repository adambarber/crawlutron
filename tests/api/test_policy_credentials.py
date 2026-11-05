"""Authorization is not a raw-header knob: a raw header bypasses the origin
scoping in crawl_policy; credentials ride policy.credentials, scoped to one
origin, and origin-keyed fields use the existing cross-host scrub.
"""

import pytest
from pydantic import SecretStr

from crawlutron.admission.credentials import origin_of
from crawlutron.admission.policy import PolicyTier, read_policy, validate_wire_policy
from crawlutron.models.crawl_policy import CrawlPolicy
from crawlutron.models.crawl_request import CrawlRequest as _CrawlRequest
from crawlutron.models.credential import Credential
from crawlutron.transport.request_profile import apply_credentials
from tests._policy_factories import canonical, payload_policy

#: The settings a payload-policy test runs against: the defaults with nothing overridden.
MODEL_DEFAULTS_TIER = PolicyTier.model_defaults()


def _wire_policy(policy: dict[str, object]) -> CrawlPolicy:
    """The wire path a caller's policy takes: the model coerces, the funnel absorbs the tier."""
    request = _CrawlRequest.model_validate({"url": "https://a.example/", "policy": policy})
    return payload_policy(request, MODEL_DEFAULTS_TIER)


class TestWireModel:
    def test_bearer_requires_a_token(self):
        with pytest.raises(ValueError):
            Credential(kind="bearer")

    def test_basic_requires_username_and_password(self):
        with pytest.raises(ValueError):
            Credential(kind="basic", username="u")

    def test_policy_accepts_origin_keyed_credentials(self):
        policy = CrawlPolicy(credentials={"https://private.example": {"kind": "bearer", "token": "t"}})
        token = policy.credentials["https://private.example"].token
        assert token is not None and token.get_secret_value() == "t"

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
        from crawlutron.transport.request_profile import headers_for_strategy

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
        policy = _wire_policy(
            {"credentials": {"HTTPS://Private.Example:443/": {"kind": "bearer", "token": "t"}}}
        )
        assert list(policy.credentials) == ["https://private.example"]
        token = policy.credentials["https://private.example"].token
        assert token is not None and token.get_secret_value() == "t"

    def test_malformed_credential_is_rejected_not_dropped(self):
        with pytest.raises(ValueError, match="credentials"):
            _wire_policy({"credentials": {"https://a.example": {"kind": "carrier"}}})

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
            validate_wire_policy(
                {
                    "credentials": {
                        "HTTPS://a.example": {"kind": "bearer", "token": "first"},
                        "https://a.example:443": {"kind": "bearer", "token": "second"},
                    }
                },
                MODEL_DEFAULTS_TIER,
            )

    def test_distinct_origins_still_validate(self):
        """The collision check must not reject a policy that merely looks similar."""
        policy = _wire_policy(
            {
                "credentials": {
                    "https://a.example": {"kind": "bearer", "token": "one"},
                    "https://b.example": {"kind": "bearer", "token": "two"},
                    "https://a.example:8443": {"kind": "bearer", "token": "three"},
                }
            }
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
        once = _wire_policy({"credentials": {"https://a.example": {"kind": "bearer", "token": "t"}}})
        twice = _wire_policy({"credentials": dict(once.credentials)})
        assert dict(twice.credentials) == dict(once.credentials)


class TestCredentialValueInjection:
    """#441: a credential is a header VALUE at one remove -- a bearer token rides
    Authorization verbatim, so the CR/LF/NUL law that governs policy.headers
    values governs every secret field too.
    """

    @pytest.mark.parametrize(
        ("kind", "field", "filler"),
        [
            ("bearer", "token", {}),
            ("basic", "username", {"password": "p"}),
            ("basic", "password", {"username": "u"}),
        ],
    )
    def test_injectable_secret_fields_are_refused(self, kind, field, filler):
        payload = {"kind": kind, field: "v\r\nX-Smuggled: 1", **filler}
        with pytest.raises(ValueError):
            Credential(**payload)

    def test_apply_credentials_guards_the_built_value_even_past_the_model(self):
        """Defense in depth at the assembly edge: a Credential that skipped
        validation (model_construct, or a future door) still cannot put CRLF
        on the wire through apply_credentials.
        """
        # model_construct skips validation on purpose: the smuggled CRLF is the
        # value the assembly edge must catch even past the model.
        smuggled = Credential.model_construct(kind="bearer", token=SecretStr("t\r\nX-Smuggled: 1"))
        with pytest.raises(ValueError):
            apply_credentials(
                {},
                canonical("https://private.example/page"),
                {"https://private.example": smuggled},
            )


class TestIpv6AndTrailingDotOrigins:
    """#518: an IPv6-literal credential origin must survive its own canonicalization.

    An unbracketed rebuild re-parses with ValueError, so every reload of a row
    carrying one fails ``read_policy`` and the row is unloadable.
    """

    def test_origin_of_brackets_ipv6_literals(self):
        origin = origin_of("https://[2001:0DB8::1]:8080/page")
        assert origin == "https://[2001:0db8::1]:8080"
        # Idempotent: the canonical form re-parses to itself.
        assert origin_of(origin) == origin

    def test_origin_of_matches_the_normalized_url_spelling(self):
        url = canonical("https://[2001:0db8::1]:8080/page")
        assert origin_of(url.raw) == url.origin

    def test_origin_of_strips_the_trailing_fqdn_dot(self):
        assert origin_of("https://example.com./page") == "https://example.com"

    def test_an_ipv6_credential_policy_revalidates(self):
        """The wire path writes origin_of's spelling into the policy; the read
        edge re-runs the canonicalizer on every load, so one spelling must
        survive its own round trip."""
        policy = _wire_policy(
            {"credentials": {"https://[2001:0db8::1]:8080": {"kind": "bearer", "token": "t"}}}
        )
        assert list(policy.credentials) == ["https://[2001:0db8::1]:8080"]
        again = _wire_policy({"credentials": dict(policy.credentials)})
        assert dict(again.credentials) == dict(policy.credentials)

    def test_an_ipv6_credential_row_loads_from_the_job_store(self, tmp_path):
        """The #518 corruption: a whole-site job whose policy carries an
        IPv6-port credential failed read_policy on EVERY load and the row
        returned None (job_store_load_corrupt)."""
        from tests._policy_factories import UNRESTRICTED_HOST_POLICY as unrestricted
        from tests._policy_factories import job_store

        store = job_store(tmp_path / "jobs.db", policy=unrestricted)
        policy = _wire_policy(
            {"credentials": {"https://[2001:0db8::1]:8080": {"kind": "bearer", "token": "t"}}}
        )
        job = store.create("https://[2001:0db8::1]:8080/", policy)
        loaded = store.load(job.job_id)
        assert loaded is not None, "an IPv6-credential row must load back"
        assert "https://[2001:0db8::1]:8080" in read_policy(loaded.policy).credentials

    def test_an_ipv6_origin_credential_attaches(self):
        headers = apply_credentials(
            {},
            canonical("https://[2001:0db8::1]:8080/page"),
            {"https://[2001:0db8::1]:8080": Credential(kind="bearer", token="t")},
        )
        assert headers["Authorization"] == "Bearer t"
