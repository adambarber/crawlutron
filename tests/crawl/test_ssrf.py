"""SSRF protection: blocked IP ranges, unresolved hosts, DNS pinning, local mode and fixture enforcement."""

import contextlib
import socket

import pytest
from tests._assertions import assert_dns_pinning_uses_first_resolution, block_private_targets, host_resolves_to

import app.crawl.strategies.pinned_transport as http_mod
from app.crawl.strategies.pinned_transport import _check_ssrf_protection, _resolve_and_pin_host
from app.validation import HostPolicy, normalize_public_http_url


def _blocked(ip):
    return http_mod._is_private_or_loopback_ip(ip)


def test_cgnat_is_blocked():
    assert _blocked("100.64.0.1") is True


def test_multicast_is_blocked():
    assert _blocked("224.0.0.1") is True
    assert _blocked("ff02::1") is True


def test_unspecified_is_blocked():
    assert _blocked("0.0.0.0") is True


def test_reserved_is_blocked():
    assert _blocked("240.0.0.1") is True


def test_ipv6_unique_local_is_blocked():
    assert _blocked("fc00::1") is True
    assert _blocked("fd12:3456::1") is True


def test_nat64_is_blocked():
    assert _blocked("64:ff9b::1") is True


def test_public_ips_still_allowed():
    assert _blocked("8.8.8.8") is False
    assert _blocked("93.184.216.34") is False
    assert _blocked("2606:4700:4700::1111") is False


def test_ipv4_mapped_ipv6_cgnat_is_blocked():
    """An IPv4-mapped IPv6 wrapper around a CGNAT address must not slip past.

    ``::ffff:100.64.0.1`` is not caught by the stdlib range predicates
    (``is_private`` is False for the mapped form of 100.64/10 on this
    interpreter), and the explicit CGNAT branch only fired for bare IPv4, so
    the mapped wrapper was classified as a public target (SSRF bypass).
    """
    assert _blocked("::ffff:100.64.0.1") is True


def test_ipv6_embedded_ipv4_is_classified_like_the_bare_ipv4():
    """Every IPv6-embedded IPv4 form must get the verdict of the IPv4 it hides.

    Covers IPv4-mapped (``::ffff:``), 6to4 (``2002::/16``) and Teredo
    (``2001::/32``). On interpreters whose stdlib range tables already catch
    the embedded address these are defence in depth; on the rest they were the
    whole bypass.
    """
    for ipv6, ipv4 in [
        ("::ffff:127.0.0.1", "127.0.0.1"),
        ("::ffff:10.1.2.3", "10.1.2.3"),
        ("::ffff:192.168.5.5", "192.168.5.5"),
        ("::ffff:100.64.0.1", "100.64.0.1"),
        ("::ffff:169.254.1.1", "169.254.1.1"),
        ("2002:7f00:1::", "127.0.0.1"),
        ("2002:64:40::", "100.64.0.1"),
    ]:
        assert _blocked(ipv6) is _blocked(ipv4), (
            f"{ipv6} verdict {_blocked(ipv6)} != bare {ipv4} verdict {_blocked(ipv4)}"
        )


def _fail_resolution(host, *args, **kwargs):
    raise socket.gaierror("name or service not known")


def test_check_ssrf_protection_blocks_unresolvable_host(monkeypatch):
    monkeypatch.setenv("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", "0")
    monkeypatch.setattr(socket, "getaddrinfo", _fail_resolution)
    with pytest.raises(ValueError):
        http_mod._check_ssrf_protection("http://nonexistent.invalid/")


def test_resolve_and_pin_host_does_not_fall_back_to_loopback(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fail_resolution)
    monkeypatch.setenv("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", "0")
    hostname, pinned_ip = http_mod._resolve_and_pin_host("http://nonexistent.invalid/")
    assert pinned_ip != "127.0.0.1", "unresolvable host must not pin to loopback"
    assert pinned_ip == "nonexistent.invalid"


def test_build_pinned_request_never_targets_loopback_for_unresolvable(monkeypatch):
    # Defense-in-depth: even if a caller skips the SSRF check, pinning must not
    # point at 127.0.0.1 for an attacker-influenced, unresolvable hostname.
    monkeypatch.setattr(socket, "getaddrinfo", _fail_resolution)
    monkeypatch.setenv("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", "0")
    # _build_pinned_request raises now (check fails closed), so assert it raises.
    with pytest.raises(ValueError):
        http_mod._build_pinned_request(
            "http://nonexistent.invalid/",
            {},
            policy=HostPolicy.unrestricted(),
        )


class TestSSRFProtection:
    def test_check_ssrf_protection_blocks_private_targets(self, monkeypatch):
        block_private_targets(monkeypatch)
        for target in ["http://localhost:8080/", "http://127.0.0.1:8080/", "http://192.168.1.1:8080/"]:
            with pytest.raises(ValueError):
                _check_ssrf_protection(target)

    def test_check_ssrf_protection_allows_public_ip(self, monkeypatch):
        # Should not raise for a public IP.
        with host_resolves_to(monkeypatch, "8.8.8.8"):
            _check_ssrf_protection("http://8.8.8.8:8080/")

    def test_resolve_and_pin_host_pins_first_public_ip(self, monkeypatch):
        with host_resolves_to(monkeypatch, "1.2.3.4"):
            hostname, ip = _resolve_and_pin_host("http://example.com:8080/")
        assert hostname == "example.com"
        assert ip == "1.2.3.4"

    def test_normalize_public_http_url_blocks_private(self, monkeypatch):
        block_private_targets(monkeypatch)
        for target in ["http://localhost:8080/", "http://127.0.0.1:8080/", "http://10.0.0.1:8080/"]:
            with pytest.raises(ValueError, match="private"):
                normalize_public_http_url(target, policy=HostPolicy.unrestricted())

    def test_hostname_resolved_only_once(self, monkeypatch):
        """The SSRF check must resolve the hostname exactly once.

        Clears the process-global DNS cache first: the resolution cache (#3)
        persists across tests, so without isolation a prior test that resolved
        the same host would serve this call from cache (0 getaddrinfo calls).
        """
        from app.crawl.strategies.pinned_transport import _DNS_CACHE

        _DNS_CACHE.clear()
        with host_resolves_to(monkeypatch, "8.8.8.8") as resolved:
            _check_ssrf_protection("http://example.com:8080/")
        assert resolved == ["example.com"]


class TestSsrfDnsPinning:
    def test_resolves_once_and_pins_ip(self, monkeypatch):
        with host_resolves_to(monkeypatch, "93.184.216.34") as resolved:
            hostname, ip = _resolve_and_pin_host("http://example.com/")
        assert hostname == "example.com"
        assert ip == "93.184.216.34"
        assert resolved == ["example.com"], "hostname should be resolved exactly once"

    def test_rebinding_second_resolution_returns_private_but_pinning_uses_first(self, monkeypatch):
        assert_dns_pinning_uses_first_resolution(monkeypatch, "127.0.0.1")

    def test_private_resolution_raises(self, monkeypatch):
        with host_resolves_to(monkeypatch, "127.0.0.1"), pytest.raises(ValueError):
            _resolve_and_pin_host("http://example.com/")

    def test_localhost_blocked_by_default(self, monkeypatch):
        block_private_targets(monkeypatch)
        with pytest.raises(ValueError):
            _check_ssrf_protection("http://localhost:8080/")


def test_ssrf_fixture_passes_when_opted_out(monkeypatch, ssrf_guard_required):
    """The happy path: opt out, then exercise the helper. Should pass."""
    monkeypatch.setenv("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", "0")
    # Call the real helper; it will fail to resolve but the call itself
    # counts as "exercised", which is what the fixture checks.
    with contextlib.suppress(Exception):
        http_mod._check_ssrf_protection("http://example.invalid/")


def test_ssrf_fixture_fails_when_opt_out_missing(monkeypatch, ssrf_guard_required):
    """If the test forgets to opt out, the fixture must fail with a clear
    message naming the missing opt-out.
    """
    # Deliberately do NOT set BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS=0.
    # The fixture must catch this.
    import os

    monkeypatch.delenv("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", raising=False)
    os.environ["BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS"] = "1"
    with pytest.raises(pytest.fail.Exception, match="SSRF guard"):
        http_mod._check_ssrf_protection("http://example.invalid/")


# Note: a test that requests ``ssrf_guard_required`` but never touches
# ``_check_ssrf_protection`` or ``_resolve_and_pin_host`` simply passes
# without exercising the SSRF path. The fixture catches opt-out misses
# in the per-call wrapper, which fires during the test body and is
# catchable via ``pytest.raises``.


def test_allow_private_returns_original_hostname_not_loopback(monkeypatch):
    monkeypatch.setenv("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", "1")
    hostname, pinned = _resolve_and_pin_host("http://example.com/")
    assert hostname == "example.com"
    # Must connect to the requested host, not hardcoded loopback.
    assert pinned == "example.com"
    assert pinned != "127.0.0.1"


def test_allow_private_preserves_host_for_webhook_style_urls(monkeypatch):
    # A non-loopback host under local dev mode stays as-is (so webhooks and
    # fetches to the real hostname are not misrouted to 127.0.0.1).
    monkeypatch.setenv("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", "1")
    hostname, pinned = _resolve_and_pin_host("https://hooks.svc.local/path")
    assert hostname == "hooks.svc.local"
    assert pinned == "hooks.svc.local"
    assert pinned != "127.0.0.1"


def test_shared_public_target_verdict_matches_reserved_range_corpus():
    from app.validation import _host_is_public, _is_public_ip_value

    blocked = {
        "10.0.0.1": True,
        "172.16.0.1": True,
        "192.168.1.1": True,
        "127.0.0.1": True,
        "169.254.1.1": True,
        "100.64.0.1": True,
        "64:ff9b::1": True,
        "fc00::1": True,
        "fd12:3456::1": True,
        "224.0.0.1": True,
        "ff02::1": True,
        "240.0.0.1": True,
        "0.0.0.0": True,
        "::1": True,
        "fe80::1": True,
        "2001:db8::1": True,
        "::ffff:100.64.0.1": True,
    }
    public = {"8.8.8.8", "93.184.216.34", "2606:4700:4700::1111", "::ffff:8.8.8.8"}
    for address, expected_blocked in blocked.items():
        assert _is_public_ip_value(address) is (not expected_blocked), address
        assert _host_is_public(address) is (not expected_blocked), address
        assert _blocked(address) is expected_blocked, address
    for address in public:
        assert _is_public_ip_value(address) is True, address
        assert _host_is_public(address) is True, address
        assert _blocked(address) is False, address
