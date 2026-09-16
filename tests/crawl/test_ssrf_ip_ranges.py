"""LOW fix (F7): the SSRF IP check must reject non-public ranges beyond
private/loopback/link-local - CGNAT (100.64.0.0/10), multicast, reserved,
unspecified, IPv6 unique-local (fc00::/7) and NAT64 (64:ff9b::/96). Previously
only is_private/is_loopback/is_link_local were checked, so e.g. 100.64.0.1 or a
multicast address slipped through.
"""

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
