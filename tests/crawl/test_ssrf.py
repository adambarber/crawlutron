"""SSRF protection: blocked IP ranges, unresolved hosts, DNS pinning, local mode and fixture enforcement."""

import contextlib
import ipaddress
import socket

import pytest

import app.transport.pinned_transport as http_mod
from app.models.normalized_url import NormalizedUrl
from app.safety.validation import HostPolicy
from app.transport.pinned_transport import check_ssrf_protection, resolve_and_pin_host
from tests._assertions import (
    assert_dns_pinning_uses_first_resolution,
    block_private_targets,
    host_resolves_to,
)
from tests._policy_factories import canonical


def _blocked(ip):
    return http_mod._is_private_or_loopback_ip(ip)


def _teredo_hiding(client: str, server: str = "192.0.2.1") -> str:
    """A Teredo address (``2001::/32``) whose reachable half is ``client``.

    Teredo reverses the client IPv4 in the low 32 bits, so a private target can
    be smuggled through a syntactically public IPv6 literal. Built here rather
    than pasted so the test data cannot drift from the encoding the stdlib
    actually decodes.
    """
    server_bits = int(ipaddress.IPv4Address(server))
    client_bits = int(ipaddress.IPv4Address(client)) ^ 0xFFFFFFFF
    packed = (0x20010000 << 96) | (server_bits << 64) | client_bits
    return str(ipaddress.IPv6Address(packed))


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
        (_teredo_hiding("127.0.0.1"), "127.0.0.1"),
        (_teredo_hiding("10.1.2.3"), "10.1.2.3"),
        (_teredo_hiding("100.64.0.1"), "100.64.0.1"),
        (_teredo_hiding("169.254.1.1"), "169.254.1.1"),
        (_teredo_hiding("192.168.5.5"), "192.168.5.5"),
        (_teredo_hiding("8.8.8.8"), "8.8.8.8"),
    ]:
        assert _blocked(ipv6) is _blocked(ipv4), (
            f"{ipv6} verdict {_blocked(ipv6)} != bare {ipv4} verdict {_blocked(ipv4)}"
        )


def _fail_resolution(host, *args, **kwargs):
    raise socket.gaierror("name or service not known")


def _unresolvable_host(monkeypatch, *, allow_private: str = "0") -> None:
    """Establish the unresolvable-host precondition every pinning test needs.

    Two facts, always together: DNS resolution fails, and the private-target
    escape hatch is set to the stated value. Stating them per test meant five
    copies of the same precondition, and a test that set only one of the two
    would still have run -- which is the failure this chokepoint removes, since
    the hatch defaults on and silently turns the fail-closed assertions into
    passthrough ones.

    ``allow_private`` exists only for the test that proves the hatch is the sole
    exception; everything else takes the default, so a test that means "closed"
    cannot spell it wrongly.
    """
    monkeypatch.setattr(socket, "getaddrinfo", _fail_resolution)
    monkeypatch.setenv("CRAWLUTRON_ALLOW_PRIVATE_TARGETS", allow_private)


def test_check_ssrf_protection_blocks_unresolvable_host(monkeypatch):
    _unresolvable_host(monkeypatch)
    with pytest.raises(ValueError):
        http_mod.check_ssrf_protection(canonical("http://nonexistent.invalid/"))


def test_resolve_and_pin_host_fails_closed_when_resolution_fails(monkeypatch):
    """An unresolvable host must be refused, not handed back for re-resolution.

    Returning ``(hostname, hostname)`` here does not merely avoid loopback: it
    gives the HTTP client permission to resolve the name a THIRD time, at connect
    time, with no validation applied to the answer. ``check_ssrf_protection``
    validated the IPs it saw, and this discards them, so an attacker who can make
    the pin-time lookup fail gets a connection to an address nothing checked.
    That is the rebinding this function exists to prevent, reached by breaking
    the pin instead of racing it.

    The refusal is a ValueError for the same reason the no-public-IP branch below
    raises one: both mean "this hostname cannot be safely connected to", and both
    are surfaced to callers as an ordinary SSRF refusal rather than a crash.
    """
    _unresolvable_host(monkeypatch)
    with pytest.raises(ValueError, match="cannot resolve hostname"):
        http_mod.resolve_and_pin_host(canonical("http://nonexistent.invalid/"))


def test_resolve_and_pin_host_escape_hatch_survives_resolution_failure(monkeypatch):
    """The local-dev escape hatch is the ONLY path that returns an unpinned host.

    ``allow_private_targets()`` exists so a developer can crawl a service on
    their own machine, which necessarily means an unresolvable-on-the-internet
    name. It is a deliberate, environment-gated bypass -- so it is pinned to the
    env var and only the env var. If a future change made this the fallback for
    an ordinary DNS failure, the test below would keep passing while the bypass
    quietly became the default.
    """
    _unresolvable_host(monkeypatch, allow_private="1")
    hostname, pinned_ip = http_mod.resolve_and_pin_host(canonical("http://service.local/"))
    assert (hostname, pinned_ip) == ("service.local", "service.local")


async def test_pinned_hop_never_targets_loopback_for_unresolvable(monkeypatch):
    # Defense-in-depth: even if a caller skips the SSRF check, pinning must not point at 127.0.0.1 for an
    # attacker-influenced, unresolvable hostname.
    _unresolvable_host(monkeypatch)
    with pytest.raises(ValueError):
        await anext(
            http_mod.apinned_hops(
                canonical("http://nonexistent.invalid/"), {}, pin=http_mod.build_pinned_request
            )
        )


def test_pinned_builder_surfaces_unresolvable_host_as_an_ssrf_refusal(monkeypatch):
    """The caller-visible contract: an unresolvable host is refused, not crashed on.

    ``build_pinned_request`` is the public entry point every pinned fetch goes
    through, and it is the thing a caller actually catches. Pinning ``resolve_and_pin_host``
    fixed the low-level helper; this pins that the refusal reaches the caller as the
    ordinary ValueError it has always been -- the same refusal
    ``check_ssrf_protection`` raises, with the same message -- and not as a
    ``socket.gaierror`` escaping a layer that documents raising ValueError.

    The assertion is deliberately on the message, not merely the type: a bare
    ``pytest.raises(ValueError)`` would also pass if an unrelated ValueError were
    raised from somewhere else in the builder, which is what a silent regression
    here would look like.
    """
    _unresolvable_host(monkeypatch)
    with pytest.raises(ValueError, match="SSRF protection: cannot resolve hostname"):
        http_mod.build_pinned_request(canonical("http://nonexistent.invalid/"), {})


class TestSSRFProtection:
    @pytest.mark.parametrize("private_ip", ["127.0.0.1", "192.168.1.1"])
    def test_check_ssrf_protection_blocks_private_targets(self, monkeypatch, private_ip):
        # The live hazard shape: an admitted public NAME whose DNS answer is private
        with host_resolves_to(monkeypatch, private_ip), pytest.raises(ValueError):
            check_ssrf_protection(canonical("http://example.com/"))

    def test_check_ssrf_protection_allows_public_ip(self, monkeypatch):
        # Should not raise for a public IP.
        with host_resolves_to(monkeypatch, "8.8.8.8"):
            check_ssrf_protection(canonical("http://8.8.8.8:8080/"))

    def test_resolve_and_pin_host_pins_first_public_ip(self, monkeypatch):
        with host_resolves_to(monkeypatch, "1.2.3.4"):
            hostname, ip = resolve_and_pin_host(canonical("http://example.com:8080/"))
        assert hostname == "example.com"
        assert ip == "1.2.3.4"

    def test_admission_blocks_private_targets(self, monkeypatch):
        block_private_targets(monkeypatch)
        for target in ["http://localhost:8080/", "http://127.0.0.1:8080/", "http://10.0.0.1:8080/"]:
            with pytest.raises(ValueError, match="private"):
                NormalizedUrl(target, policy=HostPolicy.unrestricted())

    def test_hostname_resolved_only_once(self, monkeypatch):
        """The SSRF check must resolve the hostname exactly once.

        Clears the process-global DNS cache first: the resolution cache
        persists across tests, so without isolation a prior test that resolved
        the same host would serve this call from cache (0 getaddrinfo calls).
        """
        from app.transport.pinned_transport import _DNS_CACHE

        _DNS_CACHE.clear()
        with host_resolves_to(monkeypatch, "8.8.8.8") as resolved:
            check_ssrf_protection(canonical("http://example.com:8080/"))
        assert resolved == ["example.com"]


class TestFrontierIdentityRefusesRejectedTargets:
    """A rejected target must get no identity, not a fallback one.

    ``_frontier_identity`` is the admission seam's dedupe helper. It asks the
    owner for a canonical identity and, when the owner returns ``None``, used to
    hand back the raw string. That silently converted a refusal into an
    admission: the cloud-metadata address, a loopback port and a host outside an
    allow-list all came back with non-``None`` identities, so they were
    persisted, scheduled and reported as accepted (202) before the engine ever
    validated them. Every other admission boundary drops the same inputs -- the
    sitemap fetch admits each ``<loc>`` where it reads it -- so falling back to
    a raw identity here would be a divergence, not a policy.
    """

    REJECTED = [
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1:8080/x",
        "http://10.0.0.5/internal",
        "http://192.168.1.1/admin",
    ]

    @pytest.mark.parametrize("target", REJECTED)
    def test_private_target_is_not_given_an_identity(self, monkeypatch, target):
        block_private_targets(monkeypatch)
        from app.admission.admission import _frontier_identity

        assert _frontier_identity(target, host_policy=HostPolicy.unrestricted()) is None

    def test_every_raw_text_shape_is_covered(self, monkeypatch):
        """The refusal must not depend on which shape the raw text arrived in.

        ``_frontier_identity`` accepts a bare string, a dict and any object
        with ``normalized_url`` -- the three shapes that can still carry
        unadmitted text. A guard that only pins the string case leaves the
        other two open.

        ``FrontierEntry`` (and the ``(key, entry)`` tuples wrapping it) is not
        in this list: its ``url`` is an admitted value by construction -- the
        storage read edge refuses bad rows before an entry can hold one -- so
        an entry carrying a rejected target is unconstructable through
        production paths and this helper takes its identity definitionally.
        """
        block_private_targets(monkeypatch)
        from app.admission.admission import _frontier_identity

        target = "http://169.254.169.254/latest/meta-data/"
        policy = HostPolicy.unrestricted()
        shapes = [target, {"url": target}, type("S", (), {"normalized_url": target})()]
        for shape in shapes:
            label = type(shape).__name__
            assert _frontier_identity(shape, host_policy=policy) is None, (
                f"{label} shape was given an identity for a rejected target"
            )

    def test_host_outside_the_allow_list_is_not_given_an_identity(self, monkeypatch):
        """The other refusal reason fails the same way.

        ``private_network_target`` and ``host_not_allowed`` are the two ways the
        owner refuses. If only the first is honoured the guard is half a guard.
        """
        block_private_targets(monkeypatch)
        from app.admission.admission import _frontier_identity

        policy = HostPolicy.from_strings("example.com", "")
        assert _frontier_identity("http://evil.test/x", host_policy=policy) is None

    def test_a_public_target_still_gets_its_identity(self, monkeypatch):
        """The other direction: returning ``None`` always would pass the above.

        An identity is the whole point of this helper, so the guard has to reject
        the refused and admit the allowed. This is what stops the fix from being
        ``return None`` on line 114.
        """
        block_private_targets(monkeypatch)
        from app.admission.admission import _frontier_identity

        got = _frontier_identity("https://example.gov/page/", host_policy=HostPolicy.unrestricted())
        # The identity is the admitted value, and its text is the page's durable identity.
        assert got is not None and got.raw == "https://example.gov/page", (
            f"a public target lost its identity: {got!r}"
        )


class TestSsrfDnsPinning:
    def test_resolves_once_and_pins_ip(self, monkeypatch):
        with host_resolves_to(monkeypatch, "93.184.216.34") as resolved:
            hostname, ip = resolve_and_pin_host(canonical("http://example.com/"))
        assert hostname == "example.com"
        assert ip == "93.184.216.34"
        assert resolved == ["example.com"], "hostname should be resolved exactly once"

    def test_rebinding_second_resolution_returns_private_but_pinning_uses_first(self, monkeypatch):
        assert_dns_pinning_uses_first_resolution(monkeypatch, "127.0.0.1")

    def test_private_resolution_raises(self, monkeypatch):
        with host_resolves_to(monkeypatch, "127.0.0.1"), pytest.raises(ValueError):
            resolve_and_pin_host(canonical("http://example.com/"))

    def test_loopback_resolution_blocked_by_default(self, monkeypatch):
        block_private_targets(monkeypatch)
        with host_resolves_to(monkeypatch, "127.0.0.1"), pytest.raises(ValueError):
            check_ssrf_protection(canonical("http://example.com/"))


@pytest.mark.usefixtures("ssrf_guard_required")
def test_ssrf_fixture_passes_when_opted_out(monkeypatch):
    """The happy path: opt out, then exercise the helper. Should pass."""
    monkeypatch.setenv("CRAWLUTRON_ALLOW_PRIVATE_TARGETS", "0")
    # Call the real helper; it will fail to resolve but the call itself counts as "exercised", which is what
    # the fixture checks.
    with contextlib.suppress(Exception):
        http_mod.check_ssrf_protection(canonical("http://example.invalid/"))


@pytest.mark.usefixtures("ssrf_guard_required")
def test_ssrf_fixture_fails_when_opt_out_missing(monkeypatch):
    """If the test forgets to opt out, the fixture must fail with a clear
    message naming the missing opt-out.
    """
    # Deliberately do NOT set CRAWLUTRON_ALLOW_PRIVATE_TARGETS=0. The fixture must catch this.
    import os

    monkeypatch.delenv("CRAWLUTRON_ALLOW_PRIVATE_TARGETS", raising=False)
    os.environ["CRAWLUTRON_ALLOW_PRIVATE_TARGETS"] = "1"
    with pytest.raises(pytest.fail.Exception, match="SSRF guard"):
        http_mod.check_ssrf_protection(canonical("http://example.invalid/"))


# Note: a test that requests ``ssrf_guard_required`` but never touches ``check_ssrf_protection`` or
# ``resolve_and_pin_host`` simply passes without exercising the SSRF path.


def test_allow_private_returns_original_hostname_not_loopback(monkeypatch):
    monkeypatch.setenv("CRAWLUTRON_ALLOW_PRIVATE_TARGETS", "1")
    hostname, pinned = resolve_and_pin_host(canonical("http://example.com/"))
    assert hostname == "example.com"
    # Must connect to the requested host, not hardcoded loopback.
    assert pinned == "example.com"
    assert pinned != "127.0.0.1"


def test_allow_private_preserves_host_for_webhook_style_urls(monkeypatch):
    # A non-loopback host under local dev mode stays as-is (so webhooks and fetches to the real hostname are
    # not misrouted to 127.0.0.1).
    monkeypatch.setenv("CRAWLUTRON_ALLOW_PRIVATE_TARGETS", "1")
    hostname, pinned = resolve_and_pin_host(canonical("https://hooks.svc.local/path"))
    assert hostname == "hooks.svc.local"
    assert pinned == "hooks.svc.local"
    assert pinned != "127.0.0.1"


def test_shared_public_target_verdict_matches_reserved_range_corpus(monkeypatch):
    from app.models.host_policy import host_egress_reason, reset_private_target_memo
    from app.safety.validation import is_public_ip_value

    # The corpus claim is about the host verdict, so the escape hatch must be off: with it on,
    # host_egress_reason admits everything by design.
    monkeypatch.setenv("CRAWLUTRON_ALLOW_PRIVATE_TARGETS", "0")
    reset_private_target_memo()

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
        assert is_public_ip_value(address) is (not expected_blocked), address
        assert (host_egress_reason(address) is None) is (not expected_blocked), address
        assert _blocked(address) is expected_blocked, address
    for address in public:
        assert is_public_ip_value(address) is True, address
        assert host_egress_reason(address) is None, address
        assert _blocked(address) is False, address
