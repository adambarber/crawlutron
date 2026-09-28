"""DNS resolution results are cached per-host for a short TTL (#3).

The validation path (_check_ssrf_protection) may reuse a cached resolution so
a crawl of N pages on one host does not issue N blocking socket.getaddrinfo
calls. The pinning path (_resolve_and_pin_host) intentionally resolves FRESH
on every call so the connection always pins a freshly re-validated public IP;
caching the pin would let a DNS rebind inside the TTL window bypass the SSRF
check (TOCTOU).
"""

import socket
import threading
import uuid
from unittest.mock import patch

import pytest

from app.crawl.strategies import pinned_transport as http_mod
from tests._thread_fanout import run_thread_fanout


def _counting_resolver(ip: str = "8.8.8.8"):
    """Return (call_counter, fake_getaddrinfo) that resolves every host to ``ip``."""
    calls = {"n": 0}

    def fake_getaddrinfo(host_, *a, **k):
        calls["n"] += 1
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 80))]

    return calls, fake_getaddrinfo


def test_ssrf_check_caches_within_ttl(monkeypatch):
    monkeypatch.setenv("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", "0")
    host = f"a-{uuid.uuid4().hex}.example.com"
    calls, fake = _counting_resolver()
    http_mod._DNS_CACHE.clear()
    with patch("socket.getaddrinfo", side_effect=fake):
        http_mod._check_ssrf_protection(f"http://{host}/p")
        http_mod._check_ssrf_protection(f"http://{host}/q")
    # second call served from cache -> getaddrinfo called exactly once
    assert calls["n"] == 1, calls


def test_resolve_pin_host_resolves_fresh_each_call(monkeypatch):
    monkeypatch.setenv("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", "0")
    host = f"b-{uuid.uuid4().hex}.example.com"
    calls, fake = _counting_resolver()
    http_mod._DNS_CACHE.clear()
    with patch("socket.getaddrinfo", side_effect=fake):
        h1, ip1 = http_mod._resolve_and_pin_host(f"http://{host}/p")
        h2, ip2 = http_mod._resolve_and_pin_host(f"http://{host}/q")
    # pin path is intentionally NOT cached (security): resolves each call
    assert calls["n"] == 2, calls
    assert ip1 == ip2 == "8.8.8.8"


def test_ssrf_check_uses_cache_and_still_blocks_private(monkeypatch):
    monkeypatch.setenv("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", "0")
    host = f"c-{uuid.uuid4().hex}.example.com"
    http_mod._DNS_CACHE.clear()
    with patch(
        "socket.getaddrinfo",
        return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))],
    ):
        # The reason matters as much as the type: this has to be the private-address
        # refusal, not any incidental ValueError the parse could raise.
        with pytest.raises(ValueError, match="resolves to private/loopback IP"):
            http_mod._check_ssrf_protection(f"http://{host}/")


class TestDnsCacheLocking:
    def test_dns_cache_lock_is_present_and_used(self):
        from app.bounded_ttl import BoundedTTLCache
        from app.crawl.strategies import pinned_transport as http_mod

        # The chokepoint moved the lock INSIDE the cache: the module no longer
        # carries a bare dict + lock pair that callers must compose correctly.
        assert isinstance(http_mod._DNS_CACHE, BoundedTTLCache)
        assert isinstance(http_mod._DNS_CACHE._lock, type(threading.Lock()))

    def test_dns_cache_concurrent_writes_do_not_corrupt(self):
        """Eight threads racing ``_resolve_ips`` must leave a consistent cache."""
        from app.crawl.strategies import pinned_transport as http_mod

        # Resolve a host that exists on every test machine
        host = "127.0.0.1"

        def worker(_index: int) -> None:
            for _ in range(50):
                http_mod._resolve_ips(host)

        errors = run_thread_fanout(worker)
        assert not errors, errors
        # Cache is consistent (not corrupted by concurrent mutation) and the
        # winning write is readable.
        assert set(http_mod._DNS_CACHE.peek(host)) == {"127.0.0.1"}
