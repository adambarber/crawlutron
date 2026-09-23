"""URL and network policy validation: schemes, private networks, registrable domains."""

from _testkit import *


class TestPolicyValidation:
    def test_unsupported_scheme(self):
        with pytest.raises(ValueError):
            normalize_public_http_url("ftp://example.com/")

    def test_private_network_blocked(self):
        saved = os.environ.get("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS")
        os.environ["BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS"] = "0"
        try:
            with pytest.raises(ValueError):
                normalize_public_http_url("http://127.0.0.1/x")
        finally:
            if saved is None:
                os.environ.pop("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", None)
            else:
                os.environ["BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS"] = saved

    def test_trailing_colon_path(self):
        assert normalize_public_http_url("https://example.com/seg:") == "https://example.com/seg"

    def test_host_allow_list_restricts_targets(self):
        assert normalize_public_http_url("https://Example.com:8443/page", allowed_hosts="example.com")
        with pytest.raises(ValueError, match="host_not_allowed"):
            normalize_public_http_url("https://other.test/page", allowed_hosts="example.com")
        with pytest.raises(ValueError, match="host_not_allowed"):
            normalize_public_http_url("https://notexample.com/page", allowed_hosts="example.com")

    def test_host_deny_list_refuses_targets(self):
        assert normalize_public_http_url("https://allowed.test/page", denied_hosts="blocked.test")
        with pytest.raises(ValueError, match="host_denied"):
            normalize_public_http_url("https://Blocked.test:443/page", denied_hosts="blocked.test")

    def test_host_deny_precedes_allow(self):
        with pytest.raises(ValueError, match="host_denied"):
            normalize_public_http_url(
                "https://blocked.test/page",
                allowed_hosts="blocked.test",
                denied_hosts="blocked.test",
            )

    def test_host_policy_normalizes_case_port_and_idna(self):
        assert normalize_public_http_url(
            "https://XN--BCHER-KVA.example:8443/page",
            allowed_hosts="BÜCHER.Example:9443",
        )
        with pytest.raises(ValueError, match="host_not_allowed"):
            normalize_public_http_url("https://other.test/page", allowed_hosts="bücher.example")

    def test_host_policy_reads_configured_environment(self, monkeypatch):
        monkeypatch.setenv("BROWSER_CRAWLER_ALLOWED_HOSTS", "allowed.test")
        monkeypatch.setenv("BROWSER_CRAWLER_DENIED_HOSTS", "blocked.test")
        assert normalize_public_http_url("https://allowed.test/page")
        with pytest.raises(ValueError, match="host_not_allowed"):
            normalize_public_http_url("https://other.test/page")
        with pytest.raises(ValueError, match="host_denied"):
            normalize_public_http_url("https://blocked.test/page")

    def test_host_policy_keeps_ssrf_guard_active(self, monkeypatch):
        monkeypatch.setenv("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", "0")
        with pytest.raises(ValueError, match="private_network_target"):
            normalize_public_http_url(
                "http://127.0.0.1/page",
                allowed_hosts="127.0.0.1",
            )

    def test_registrable_domain(self):
        assert registrable_domain("https://a.b.c.example.com/x") == "example.com"
        assert registrable_domain("https://example.com/x") == "example.com"

    def test_invalid_url_no_host(self):
        with pytest.raises(ValueError):
            normalize_public_http_url("http:///path")

    def test_private_network_localhost_blocked(self):
        saved = os.environ.get("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS")
        os.environ["BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS"] = "0"
        try:
            with pytest.raises(ValueError):
                normalize_public_http_url("http://localhost/x")
        finally:
            if saved is None:
                os.environ.pop("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", None)
            else:
                os.environ["BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS"] = saved

    def test_private_network_normal_domain_passed_through(self):
        saved = os.environ.get("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS")
        os.environ["BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS"] = "0"
        try:
            out = normalize_public_http_url("http://example.com/x")
            assert out == "http://example.com/x"
        finally:
            if saved is None:
                os.environ.pop("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", None)
            else:
                os.environ["BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS"] = saved

    def test_registrable_domain_two_part_suffix(self):
        assert registrable_domain("https://a.b.example.co.uk/x") == "example.co.uk"

    def test_integer_ip_literal_blocked(self, monkeypatch):
        # L4: resolvers read bare integers/hex as IPv4; layer 1 must too.
        monkeypatch.setenv("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", "0")
        with pytest.raises(ValueError):
            normalize_public_http_url("http://2130706433/")  # 127.0.0.1
        with pytest.raises(ValueError):
            normalize_public_http_url("http://0x7f000001/")  # 127.0.0.1
        with pytest.raises(ValueError):
            normalize_public_http_url("http://127.1/")  # 127.0.0.1

    def test_public_integer_literal_allowed(self, monkeypatch):
        # Numeric but public (1.1.1.1) stays reachable.
        monkeypatch.setenv("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", "0")
        assert normalize_public_http_url("http://16843009/") == "http://16843009"

    def test_wrapping_integer_to_loopback_blocked(self, monkeypatch):
        # Oversized integers wrap mod 2**32 on resolvers: this one lands on
        # 127.0.0.1 and must be rejected, not passed through as a hostname.
        monkeypatch.setenv("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", "0")
        with pytest.raises(ValueError):
            normalize_public_http_url("http://6425673729/")

    def test_registrable_domain_multi_level_suffixes(self):
        # M5: the hand-rolled 7-entry suffix set collapsed every unlisted
        # two-part suffix to itself (a.co.jp and b.co.jp both -> co.jp),
        # sharing gates across unrelated sites. The real PSL keeps them distinct.
        assert registrable_domain("https://a.co.jp/x") == "a.co.jp"
        assert registrable_domain("https://b.co.jp/x") == "b.co.jp"
        assert registrable_domain("https://www.example.com.br/x") == "example.com.br"
        assert registrable_domain("https://shop.example.gov.in/x") == "example.gov.in"

    def test_registrable_domain_falls_back_without_dataset(self, monkeypatch):
        # The heuristic fallback must keep working when the dataset is
        # absent (import safety), even though correctness needs the PSL.
        import sys

        import app.validation as validation

        # NOTE: the loader caches into module-global _PSL (including the
        # failed None). monkeypatch.delattr cannot restore a never-existing
        # attribute, so save/restore explicitly -- otherwise the cached None
        # poisons every later PSL test in the session (order-dependent).
        had_psl = "_PSL" in vars(validation)
        old_psl = vars(validation).get("_PSL")
        try:
            monkeypatch.setitem(sys.modules, "publicsuffixlist", None)
            if had_psl:
                monkeypatch.delattr(validation, "_PSL")
            assert validation.registrable_domain("https://a.b.example.co.uk/x") == "example.co.uk"
            assert validation.registrable_domain("https://a.b.c.example.com/x") == "example.com"
        finally:
            if had_psl:
                validation._PSL = old_psl
            else:
                vars(validation).pop("_PSL", None)


class TestPublicUrlOrNone:
    """Egress filter: resolve-then-strict-accept (js_endpoints/next-page)."""

    def test_relative_resolves_against_source(self):
        assert (
            public_url_or_none("/api/x", resolve_against="https://example.gov/p")
            == "https://example.gov/api/x"
        )

    def test_protocol_relative_resolves_then_accepts(self):
        assert (
            public_url_or_none("//cdn.example.gov/x", resolve_against="https://example.gov/p")
            == "https://cdn.example.gov/x"
        )

    def test_absolute_http_url_passes_without_base(self):
        assert public_url_or_none("https://example.gov/a") == "https://example.gov/a"

    def test_unresolvable_relative_is_none(self):
        assert public_url_or_none("/api/x") is None

    def test_non_http_schemes_are_none(self):
        assert public_url_or_none("javascript:alert(1)", resolve_against="https://example.gov/") is None
        assert public_url_or_none("data:text/html,x", resolve_against="https://example.gov/") is None
        assert public_url_or_none("ftp://example.gov/x") is None

    def test_empty_is_none(self):
        assert public_url_or_none("") is None
        assert public_url_or_none("   ") is None


async def test_crawl_request_applies_settings_host_policy():
    from app.crawl.engine import crawl_request
    from app.settings import CrawlerSettings

    settings = CrawlerSettings(allowed_hosts="allowed.test")
    with pytest.raises(ValueError, match="host_not_allowed"):
        await crawl_request(
            {
                "url": "https://blocked.test/page",
                "policy": {"strategies": ["HTTP_DECLARED_CRAWLER"]},
            },
            settings=settings,
        )
