"""URL and network policy validation: schemes, private networks, registrable domains."""

import pytest

from app.models.host_policy import HostPolicy
from app.models.normalized_url import NormalizedUrl
from tests._policy_factories import UNRESTRICTED_HOST_POLICY
from tests._validation_helpers import admit_or_none, assert_url_refused, registrable_domain


class TestPolicyValidation:
    def test_unsupported_scheme(self):
        with pytest.raises(ValueError):
            NormalizedUrl("ftp://example.com/", policy=UNRESTRICTED_HOST_POLICY)

    def test_private_network_blocked(self, monkeypatch):
        monkeypatch.setenv("CRAWLUTRON_ALLOW_PRIVATE_TARGETS", "0")
        with pytest.raises(ValueError):
            NormalizedUrl("http://127.0.0.1/x", policy=UNRESTRICTED_HOST_POLICY)

    def test_trailing_colon_path(self):
        assert (
            NormalizedUrl("https://example.com/seg:", policy=UNRESTRICTED_HOST_POLICY).raw
            == "https://example.com/seg"
        )

    def test_host_allow_list_restricts_targets(self):
        policy = HostPolicy.from_strings("example.com", "")
        assert NormalizedUrl("https://Example.com:8443/page", policy=policy).raw
        assert_url_refused("https://other.test/page", policy, "host_not_allowed")
        assert_url_refused("https://notexample.com/page", policy, "host_not_allowed")

    def test_host_deny_list_refuses_targets(self):
        policy = HostPolicy.from_strings("", "blocked.test")
        assert NormalizedUrl("https://allowed.test/page", policy=policy).raw
        assert_url_refused("https://Blocked.test:443/page", policy, "host_denied")

    def test_host_deny_precedes_allow(self):
        policy = HostPolicy.from_strings("blocked.test", "blocked.test")
        assert_url_refused("https://blocked.test/page", policy, "host_denied")

    def test_host_policy_normalizes_case_port_and_idna(self):
        policy = HostPolicy.from_strings("BÜCHER.Example:9443", "")
        assert NormalizedUrl(
            "https://XN--BCHER-KVA.example:8443/page",
            policy=policy,
        )
        assert_url_refused("https://other.test/page", policy, "host_not_allowed")

    def test_host_policy_constructed_from_settings(self):
        from app.config.settings import CrawlerSettings

        settings = CrawlerSettings.from_mapping(
            {
                "CRAWLUTRON_ALLOWED_HOSTS": "allowed.test",
                "CRAWLUTRON_DENIED_HOSTS": "blocked.test",
            }
        )
        policy = HostPolicy.from_settings(settings)
        assert NormalizedUrl("https://allowed.test/page", policy=policy).raw
        assert_url_refused("https://other.test/page", policy, "host_not_allowed")
        assert_url_refused("https://blocked.test/page", policy, "host_denied")

    def test_host_policy_keeps_ssrf_guard_active(self, monkeypatch):
        monkeypatch.setenv("CRAWLUTRON_ALLOW_PRIVATE_TARGETS", "0")
        assert_url_refused(
            "http://127.0.0.1/page", HostPolicy.from_strings("127.0.0.1", ""), "private_network_target"
        )

    def test_registrable_domain(self):
        assert registrable_domain("a.b.c.example.com") == "example.com"
        assert registrable_domain("example.com") == "example.com"

    def test_invalid_url_no_host(self):
        with pytest.raises(ValueError):
            NormalizedUrl("http:///path", policy=UNRESTRICTED_HOST_POLICY)

    def test_private_network_localhost_blocked(self, monkeypatch):
        monkeypatch.setenv("CRAWLUTRON_ALLOW_PRIVATE_TARGETS", "0")
        with pytest.raises(ValueError):
            NormalizedUrl("http://localhost/x", policy=UNRESTRICTED_HOST_POLICY)

    def test_private_network_normal_domain_passed_through(self, monkeypatch):
        monkeypatch.setenv("CRAWLUTRON_ALLOW_PRIVATE_TARGETS", "0")
        out = NormalizedUrl("http://example.com/x", policy=UNRESTRICTED_HOST_POLICY)
        assert out.raw == "http://example.com/x"

    def test_registrable_domain_two_part_suffix(self):
        assert registrable_domain("a.b.example.co.uk") == "example.co.uk"

    def test_integer_ip_literal_blocked(self, monkeypatch):
        # Resolvers read bare integers/hex as IPv4; layer 1 must too.
        monkeypatch.setenv("CRAWLUTRON_ALLOW_PRIVATE_TARGETS", "0")
        with pytest.raises(ValueError):
            NormalizedUrl("http://2130706433/", policy=UNRESTRICTED_HOST_POLICY)  # 127.0.0.1
        with pytest.raises(ValueError):
            NormalizedUrl("http://0x7f000001/", policy=UNRESTRICTED_HOST_POLICY)  # 127.0.0.1
        with pytest.raises(ValueError):
            NormalizedUrl("http://127.1/", policy=UNRESTRICTED_HOST_POLICY)  # 127.0.0.1

    def test_public_integer_literal_allowed(self, monkeypatch):
        # Numeric but public (1.1.1.1) stays reachable.
        monkeypatch.setenv("CRAWLUTRON_ALLOW_PRIVATE_TARGETS", "0")
        assert NormalizedUrl("http://16843009/", policy=UNRESTRICTED_HOST_POLICY).raw == "http://16843009"

    def test_wrapping_integer_to_loopback_blocked(self, monkeypatch):
        # Oversized integers wrap mod 2**32 on resolvers: this one lands on 127.0.0.1 and must be rejected,
        # not passed through as a hostname.
        monkeypatch.setenv("CRAWLUTRON_ALLOW_PRIVATE_TARGETS", "0")
        with pytest.raises(ValueError):
            NormalizedUrl("http://6425673729/", policy=UNRESTRICTED_HOST_POLICY)

    def test_registrable_domain_multi_level_suffixes(self):
        # The hand-rolled 7-entry suffix set collapsed every unlisted two-part suffix to itself (a.co.jp and
        # b.co.jp both -> co.jp), sharing gates across unrelated sites. The real PSL keeps them distinct.
        assert registrable_domain("a.co.jp") == "a.co.jp"
        assert registrable_domain("b.co.jp") == "b.co.jp"
        assert registrable_domain("www.example.com.br") == "example.com.br"
        assert registrable_domain("shop.example.gov.in") == "example.gov.in"

    def test_registrable_domain_falls_back_without_dataset(self):
        # The heuristic fallback must keep working when the dataset is absent (import safety), even though
        # correctness needs the PSL. Passing ``psl=None`` forces the fallback without touching module state.
        assert registrable_domain("a.b.example.co.uk", psl=None) == "example.co.uk"
        assert registrable_domain("a.b.c.example.com", psl=None) == "example.com"

    def test_public_suffix_detection(self):
        from app.models.registrable_domain import is_public_suffix

        # The cookie admission edge refuses a Domain= naming one of these
        # (#438): every TLD, and every listed multi-label suffix.
        assert is_public_suffix("com")
        assert is_public_suffix("co.uk")
        assert not is_public_suffix("example.com")
        assert not is_public_suffix("example.co.uk")

    def test_public_suffix_detection_falls_back_without_dataset(self):
        from app.models.registrable_domain import is_public_suffix

        # Without the dataset every single label is still a TLD, and the known
        # two-part suffixes stay suffixes (the heuristic's whole fidelity).
        assert is_public_suffix("com", psl=None)
        assert is_public_suffix("co.uk", psl=None)
        assert not is_public_suffix("example.com", psl=None)


class TestResolveEgress:
    """Egress filter: resolve-then-strict-accept (js_endpoints/next-page)."""

    def test_relative_resolves_against_source(self):
        base = NormalizedUrl("https://example.gov/p", policy=UNRESTRICTED_HOST_POLICY)
        assert base.resolve("/api/x").raw == "https://example.gov/api/x"

    def test_protocol_relative_resolves_then_accepts(self):
        base = NormalizedUrl("https://example.gov/p", policy=UNRESTRICTED_HOST_POLICY)
        assert base.resolve("//cdn.example.gov/x").raw == "https://cdn.example.gov/x"

    def test_absolute_http_url_admits_without_base(self):
        assert admit_or_none("https://example.gov/a").raw == "https://example.gov/a"

    def test_unresolvable_relative_without_base_is_none(self):
        assert admit_or_none("/api/x") is None

    def test_non_http_schemes_are_none(self):
        base = NormalizedUrl("https://example.gov/", policy=UNRESTRICTED_HOST_POLICY)
        assert base.resolve("javascript:alert(1)") is None
        assert base.resolve("data:text/html,x") is None
        assert admit_or_none("ftp://example.gov/x") is None

    def test_empty_is_none(self):
        assert admit_or_none("") is None
        assert admit_or_none("   ") is None


async def test_crawl_request_applies_settings_host_policy():
    from app.config.settings import CrawlerSettings
    from app.orchestration.engine import crawl_request

    settings = CrawlerSettings(allowed_hosts="allowed.test")
    with pytest.raises(ValueError, match="host_not_allowed"):
        await crawl_request(
            {
                "url": "https://blocked.test/page",
                "policy": {"strategies": ["HTTP_DECLARED_CRAWLER"]},
            },
            settings=settings,
        )
