'''LOW fix (F7): the SSRF IP check must reject non-public ranges beyond
private/loopback/link-local - CGNAT (100.64.0.0/10), multicast, reserved,
unspecified, IPv6 unique-local (fc00::/7) and NAT64 (64:ff9b::/96). Previously
only is_private/is_loopback/is_link_local were checked, so e.g. 100.64.0.1 or a
multicast address slipped through.
'''
import app.crawl.strategies.http as http_mod


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
