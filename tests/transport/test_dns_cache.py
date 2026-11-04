"""DNS resolution results are cached per-host for a short TTL.

The validation path reuses a cached resolution to avoid N blocking lookups;
the pinning path resolves FRESH so a rebind inside the TTL cannot bypass SSRF.
"""

import socket
import uuid
from unittest.mock import patch

import pytest

from crawlutron.transport import pinned_dns as http_mod
from tests._policy_factories import canonical
from tests._thread_fanout import run_thread_fanout


def _counting_resolver(ip: str = "8.8.8.8"):
    """Return (call_counter, fake_getaddrinfo) that resolves every host to ``ip``."""
    calls = {"n": 0}

    def fake_getaddrinfo(_host, *a, **k):
        calls["n"] += 1
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 80))]

    return calls, fake_getaddrinfo


def test_ssrf_check_caches_within_ttl(monkeypatch):
    monkeypatch.setenv("CRAWLUTRON_ALLOW_PRIVATE_TARGETS", "0")
    host = f"a-{uuid.uuid4().hex}.example.com"
    calls, fake = _counting_resolver()
    http_mod.reset_process_state()
    with patch("socket.getaddrinfo", side_effect=fake):
        http_mod.check_ssrf_protection(canonical(f"http://{host}/p"))
        http_mod.check_ssrf_protection(canonical(f"http://{host}/q"))
    # second call served from cache -> getaddrinfo called exactly once
    assert calls["n"] == 1, calls


def test_resolve_pin_host_resolves_fresh_each_call(monkeypatch):
    monkeypatch.setenv("CRAWLUTRON_ALLOW_PRIVATE_TARGETS", "0")
    host = f"b-{uuid.uuid4().hex}.example.com"
    calls, fake = _counting_resolver()
    http_mod.reset_process_state()
    with patch("socket.getaddrinfo", side_effect=fake):
        h1, ip1 = http_mod.resolve_and_pin_host(canonical(f"http://{host}/p"))
        h2, ip2 = http_mod.resolve_and_pin_host(canonical(f"http://{host}/q"))
    # pin path is intentionally NOT cached (security): resolves each call
    assert calls["n"] == 2, calls
    assert ip1 == ip2 == "8.8.8.8"


def test_ssrf_check_uses_cache_and_still_blocks_private(monkeypatch):
    monkeypatch.setenv("CRAWLUTRON_ALLOW_PRIVATE_TARGETS", "0")
    host = f"c-{uuid.uuid4().hex}.example.com"
    http_mod.reset_process_state()
    with patch(
        "socket.getaddrinfo",
        return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))],
    ):
        # The reason matters as much as the type: this has to be the private-address refusal, not any
        # incidental ValueError the parse could raise.
        with pytest.raises(ValueError, match="resolves to private/loopback IP"):
            http_mod.check_ssrf_protection(canonical(f"http://{host}/"))


class TestDnsCacheLocking:
    def test_dns_cache_concurrent_writes_do_not_corrupt(self, monkeypatch):
        """Eight threads racing the public check must leave a consistent cache.

        The cache is process-wide; concurrent first resolutions must not corrupt
        it, and every thread gets the same answer.
        """
        monkeypatch.setenv("CRAWLUTRON_ALLOW_PRIVATE_TARGETS", "0")
        host = f"d-{uuid.uuid4().hex}.example.com"
        with patch(
            "socket.getaddrinfo",
            return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 80))],
        ):

            def worker(_index: int) -> None:
                for _ in range(50):
                    http_mod.check_ssrf_protection(canonical(f"http://{host}/"))

            errors = run_thread_fanout(worker)
            assert not errors, errors
