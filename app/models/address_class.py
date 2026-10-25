"""Address classification: parse once, judge once, answer many questions.
Every IP verdict needs the same two steps (parse the string, unwrap an IPv4 inside
IPv6, read the range predicates), so the ranges are classified here and the questions asked elsewhere.
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
_NAT64_NETWORKS = [
    ipaddress.ip_network("64:ff9b::/96"),
    # RFC 8215 local-use prefix; same fall-through hazard as the /96.
    ipaddress.ip_network("64:ff9b:1::/48"),
]


class AddressClass(StrEnum):
    """Which range an address falls in, or that it is not an address at all.
    NOT_AN_ADDRESS is a first-class answer, not a failure: folding "could not
    parse" into PUBLIC would silently invert every consumer's decision.
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
    """Interpret *host* as IPv4 the way resolvers parse numeric forms: ipaddress
    rejects bare integers, hex and short dotted quads that resolvers accept, so the
    packed value is classified itself. Returns the 32-bit value, or None when not a numeric form.
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
    """Unwrap an IPv4 hidden inside an IPv6 wrapper: IPv4-mapped, 6to4 and Teredo
    all reach an IPv4 target, so the verdict must be on the embedded IPv4, else a
    wrapper hides what the range check rejects. None (native IPv6, bare IPv4) = classify ``addr`` as-is.
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
    An already-parsed address returns unchanged, so a caller holding one does
    not re-parse and cannot reach a different answer than a string holder.
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
    """Return the range *host* falls in -- the one place an address is parsed,
    unwrapped and read against a range. Precedence is the order of the checks below
    (first match wins, PUBLIC is the fallback); private/reserved are both "not PUBLIC".
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
    if isinstance(classified, ipaddress.IPv6Address) and any(
        classified in network for network in _NAT64_NETWORKS
    ):
        return AddressClass.NAT64
    if classified.is_reserved:
        return AddressClass.RESERVED
    if classified.is_private:
        return AddressClass.PRIVATE
    return AddressClass.PUBLIC


def is_loopback_bind(host: str) -> bool:
    """Return whether a listen address keeps the service on this machine.
    Asked separately from the egress question: a private-range address is LAN-reachable,
    so reusing egress would call it local. 127.0.0.0/8, ::1 and localhost qualify; unclassifiable = reachable.
    """
    candidate = (host or "").strip()
    if candidate.lower() == "localhost":
        return True
    return classify(candidate) is AddressClass.LOOPBACK
