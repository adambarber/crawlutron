"""Address classification, and the two different questions asked of it: the SSRF
guard, the pinning path and the bind check are not the same predicate, but they
share the parse/unwrap/range work, which lives once so a range added is seen by all.
"""

import ipaddress

import pytest

from app.models.address_class import AddressClass, classify, is_loopback_bind

# One representative per class. Order matters: the classifier returns the first match, so this table doubles
# as the precedence document.
BY_CLASS = [
    (AddressClass.LOOPBACK, ["127.0.0.1", "127.0.0.53", "::1"]),
    (AddressClass.PRIVATE, ["10.0.0.1", "172.16.0.1", "192.168.1.1"]),
    (AddressClass.LINK_LOCAL, ["169.254.1.1", "fe80::1"]),
    (AddressClass.CGNAT, ["100.64.0.1"]),
    (AddressClass.UNIQUE_LOCAL, ["fc00::1", "fd12:3456::1"]),
    (AddressClass.NAT64, ["64:ff9b::1"]),
    (AddressClass.MULTICAST, ["224.0.0.1", "ff02::1"]),
    (AddressClass.UNSPECIFIED, ["0.0.0.0"]),
    (AddressClass.RESERVED, ["240.0.0.1"]),
    (AddressClass.PUBLIC, ["8.8.8.8", "93.184.216.34", "2606:4700:4700::1111"]),
]


@pytest.mark.parametrize(("expected", "addresses"), BY_CLASS, ids=[c.name for c, _ in BY_CLASS])
def test_each_range_reports_its_own_class(expected, addresses):
    for address in addresses:
        assert classify(address) is expected, address


def test_a_wrapped_ipv4_is_classified_as_the_address_it_reaches():
    """A wrapper must not hide the verdict of the address inside it.

    This is the whole reason unwrapping happens before any range is read: an
    IPv4-mapped carrier-grade NAT address is otherwise judged as public IPv6.
    """
    assert classify("::ffff:100.64.0.1") is AddressClass.CGNAT
    assert classify("::ffff:10.0.0.1") is AddressClass.PRIVATE
    # A wrapper around a genuinely public address is still public.
    assert classify("::ffff:8.8.8.8") is AddressClass.PUBLIC


def test_6to4_and_teredo_wrappers_reach_the_embedded_ipv4():
    assert classify("2002:0a00:0001::1") is AddressClass.PRIVATE


def test_a_resolver_numeric_form_is_classified_not_treated_as_a_name():
    """Resolvers accept forms ``ipaddress`` rejects, so they must be read: an
    unparseable string that is really numeric is exactly where treating it as a
    hostname would let ``127.0.0.1`` through as an unrecognised name.
    """
    assert classify("2130706433") is AddressClass.LOOPBACK
    assert classify("0x7f000001") is AddressClass.LOOPBACK
    assert classify("127.1") is AddressClass.LOOPBACK
    assert classify("16777343") is AddressClass.PUBLIC


def test_an_oversized_resolver_integer_wraps_like_a_resolver_does():
    assert classify(str(2**32 + 0x7F000001)) is AddressClass.LOOPBACK


@pytest.mark.parametrize("host", ["example.com", "", "not-an-address", "999.999.999.999"])
def test_a_name_is_not_an_address(host):
    """A host that is not an address has no range verdict, and says so.

    Returning PUBLIC here would be the dangerous answer: it would mean "we
    could not parse it, so allow it".
    """
    assert classify(host) is AddressClass.NOT_AN_ADDRESS


def test_an_oversized_quad_is_not_silently_truncated_into_range():
    """``256.0.0.1`` must not be read as ``0.0.0.1`` and land in UNSPECIFIED.

    Truncation would still reject it, but for the wrong reason -- and the same
    truncation on a public address would accept one that was never asked about.
    """
    assert classify("256.0.0.1") is AddressClass.NOT_AN_ADDRESS


def test_an_already_parsed_address_is_accepted_as_well_as_a_string():
    """Callers that already hold an address should not re-parse it.

    Both forms must agree, or a consumer that parses early and one that parses
    late would classify the same address differently.
    """
    assert classify(ipaddress.ip_address("100.64.0.1")) is AddressClass.CGNAT
    assert classify(ipaddress.ip_address("8.8.8.8")) is AddressClass.PUBLIC


@pytest.mark.parametrize(
    ("host", "local"),
    [
        ("127.0.0.1", True),
        # The whole 127/8 block is loopback, not just the one address.
        ("127.0.0.53", True),
        ("::1", True),
        ("localhost", True),
        ("LOCALHOST", True),
        ("  127.0.0.1  ", True),
        ("0.0.0.0", False),
        ("::", False),
        ("10.0.0.5", False),
        ("192.168.1.10", False),
        # A name is not known to be local, and must not be assumed.
        ("example.internal", False),
        ("not-an-address", False),
    ],
)
def test_bind_locality_is_a_separate_question_from_egress(host, local):
    assert is_loopback_bind(host) is local


def test_a_private_bind_is_not_local_even_though_it_is_not_public():
    """The distinction the two predicates exist to keep: ``10.0.0.5`` fails the public
    egress test but passes a bind-locality test; answering the bind question with the
    egress predicate would call a LAN-exposed API local.
    """
    assert classify("10.0.0.5") is AddressClass.PRIVATE
    assert is_loopback_bind("10.0.0.5") is False
