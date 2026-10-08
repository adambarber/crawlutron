"""Address classification: parse once, judge once, answer many questions.

Every IP verdict in this codebase needs the same two steps -- turn the string
into an address, unwrap any IPv4 hidden inside an IPv6 wrapper, and read the
range predicates. Those steps used to be written out at each call site, which
meant a range added for one consumer was invisible to the others.

What sits on top is not one question but several, and they do not agree:

* Is this address safe to *send a request to*? (``10.0.0.1`` -- no.)
* Is a socket bound to this address reachable *from elsewhere*?
  (``10.0.0.5`` -- yes, from every host on the LAN, even though it fails the
  first question.)

So the ranges are classified here and the questions are asked elsewhere. This
module answers exactly one of them -- which range is this -- and every caller
builds its own predicate on the answer. A consumer that wanted a sixth range
adds it here, where all the other consumers will see it.
"""

from __future__ import annotations

import ipaddress
from enum import StrEnum

__all__ = [
    "AddressClass",
    "classify",
    "is_loopback_bind",
]

# Ranges that ``ipaddress`` does not model as private and that still must not
# be reached by a public egress, nor bound by a service expecting to be local.
_CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")
_IPV6_UNIQUE_LOCAL_NETWORK = ipaddress.ip_network("fc00::/7")
_NAT64_NETWORK = ipaddress.ip_network("64:ff9b::/96")


class AddressClass(StrEnum):
    """Which range an address falls in, or that it is not an address at all.

    ``NOT_AN_ADDRESS`` is a first-class answer, not a failure. A host that
    parses as nothing has no range verdict, and the consumers each decide what
    that means for them -- an egress guard must reject it, a bind check must
    assume it is reachable. Folding "could not parse" into ``PUBLIC`` would
    silently invert both.
    """

    PUBLIC = "public"
    PRIVATE = "private"
    LOOPBACK = "loopback"
    LINK_LOCAL = "link-local"
    CGNAT = "cgnat"
    UNIQUE_LOCAL = "unique-local"
    NAT64 = "nat64"
    MULTICAST = "multicast"
    UNSPECIFIED = "unspecified"
    RESERVED = "reserved"
    NOT_AN_ADDRESS = "not-an-address"


def _resolver_numeric_part(part: str) -> int | None:
    """Parse one numeric IP component the way glibc-style resolvers do."""
    if not part:
        return None
    lowered = part.lower()
    if lowered.startswith("0x"):
        digits = lowered[2:]
        if not digits or any(c not in "0123456789abcdef" for c in digits):
            return None
        return int(digits, 16)
    if len(part) > 1 and part.startswith("0"):
        if any(c not in "01234567" for c in part):
            return None
        return int(part, 8)
    if not part.isdigit():
        return None
    return int(part, 10)


def _resolver_numeric_ipv4(host: str) -> int | None:
    """Interpret *host* as IPv4 the way resolvers parse numeric forms.

    ``ipaddress`` rejects bare integers (``2130706433``), hex
    (``0x7f000001``), and short dotted quads (``127.1``), but resolvers
    accept them -- so this must classify the packed value itself instead of
    treating every unparseable host as a plain hostname. Returns the 32-bit
    value, or None when the host is not a numeric form (or is out of range,
    which resolvers also reject).
    """
    if not host or any(c not in "0123456789abcdefxABCDEF." for c in host):
        return None
    if "." not in host:
        value = _resolver_numeric_part(host)
        if value is None:
            return None
        # Oversized single integers wrap mod 2**32 on resolvers (verified
        # against getaddrinfo); classify the effective address, not None.
        return value & 0xFFFFFFFF
    parts = host.split(".")
    if not 2 <= len(parts) <= 4:
        return None
    values = []
    for part in parts:
        parsed = _resolver_numeric_part(part)
        if parsed is None:
            return None
        values.append(parsed)
    width = len(values)
    for leading in values[:-1]:
        if leading > 0xFF:
            return None
    last_limit = 1 << (8 * (5 - width))
    if values[-1] >= last_limit:
        return None
    result = 0
    for index, value in enumerate(values[:-1]):
        result |= value << (8 * (3 - index))
    return result | values[-1]


def _embedded_ipv4(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """Unwrap an IPv4 address hidden inside an IPv6 wrapper.

    IPv4-mapped (``::ffff:0:0/96``), 6to4 (``2002::/16``) and Teredo
    (``2001::/32``) all reach an IPv4 target, so the verdict must be taken on
    the embedded IPv4 rather than the wrapper. Otherwise a wrapper hides
    whatever the IPv4 range check rejects: ``::ffff:100.64.0.1`` wrapped a
    carrier-grade NAT target and was classified public. Returns ``None`` for
    native IPv6 and bare IPv4, meaning "classify ``addr`` as-is".
    """
    if not isinstance(addr, ipaddress.IPv6Address):
        return None
    embedded = addr.ipv4_mapped or addr.sixtofour
    if embedded is None and addr.teredo is not None:
        # Teredo exposes (server, client); the client is the reachable target.
        embedded = addr.teredo[1]
    return embedded


def _parse(
    host: str | ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Return the address *host* denotes, or None if it denotes none.

    An already-parsed address is returned unchanged, so a caller holding one
    does not pay to re-parse and cannot reach a different answer than a caller
    holding the string.
    """
    if isinstance(host, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
        return host
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        # Resolver-numeric forms ipaddress rejects: classify the packed value
        # with the same predicates.
        numeric = _resolver_numeric_ipv4(host)
        return ipaddress.ip_address(numeric) if numeric is not None else None


def classify(host: str | ipaddress.IPv4Address | ipaddress.IPv6Address) -> AddressClass:
    """Return the range *host* falls in.

    The one place an address is parsed, unwrapped and read against a range.
    Precedence is the order of the checks below: the first matching range
    wins, and ``PUBLIC`` is the fallback for anything unmatched.

    A range Python models as private is reported PRIVATE and one it models as
    reserved is reported RESERVED, including the documentation block
    (``2001:db8::/32``), which Python considers private. Re-labelling those
    would be inventing a distinction no consumer makes: every one of them
    blocks on "not PUBLIC".
    """
    candidate = host.strip() if isinstance(host, str) else host
    address = _parse(candidate)
    if address is None:
        return AddressClass.NOT_AN_ADDRESS
    classified = _embedded_ipv4(address) or address
    if classified.is_loopback:
        return AddressClass.LOOPBACK
    if classified.is_unspecified:
        return AddressClass.UNSPECIFIED
    if classified.is_link_local:
        return AddressClass.LINK_LOCAL
    if classified.is_multicast:
        return AddressClass.MULTICAST
    if isinstance(classified, ipaddress.IPv4Address) and classified in _CGNAT_NETWORK:
        return AddressClass.CGNAT
    if isinstance(classified, ipaddress.IPv6Address) and classified in _IPV6_UNIQUE_LOCAL_NETWORK:
        return AddressClass.UNIQUE_LOCAL
    if isinstance(classified, ipaddress.IPv6Address) and classified in _NAT64_NETWORK:
        return AddressClass.NAT64
    if classified.is_reserved:
        return AddressClass.RESERVED
    if classified.is_private:
        return AddressClass.PRIVATE
    return AddressClass.PUBLIC


def is_loopback_bind(host: str) -> bool:
    """Return whether a listen address keeps the service on this machine.

    The bind question, asked separately from the egress question on purpose.
    A private-range address is not a public egress target and is still
    reachable from the LAN, so reusing the egress verdict to answer this would
    call a LAN-exposed service local.

    ``127.0.0.0/8`` and ``::1`` qualify, as does the name ``localhost``.
    Anything else -- including a host this module cannot classify -- is
    reported reachable, because the failure mode of getting this wrong is a
    service listening where nobody was told it would.
    """
    candidate = (host or "").strip()
    if candidate.lower() == "localhost":
        return True
    return classify(candidate) is AddressClass.LOOPBACK
