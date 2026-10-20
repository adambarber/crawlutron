"""Adversarial fixes (MEDIUM): CookieJar thread-safety and atomic persistence.

The global jar is mutated concurrently; a crash during a direct ``save_to_file``
could leave a truncated jar that silently lost all persisted cookies.
"""

import contextlib
import json
import os
import tempfile
from unittest.mock import patch

from app.transport.cookie_jar import CookieJar, global_cookie_jar
from tests._cookie_helpers import cookie_file, read_cookie_file
from tests._policy_factories import canonical
from tests._thread_fanout import run_thread_fanout


def test_cookie_jar_delegates_through_one_thread_safe_medium():
    # The jar holds no lock of its own; the bounded medium owns the single guard. That
    # is observable only through behavior: the LRU cap and per-host eviction hold.
    jar = CookieJar()
    cap = _cookie_jar_max_hosts()
    for i in range(cap + 3):
        jar.add_cookie_header(canonical(f"http://h{i}.example/"), f"k=v{i}; Path=/")
    # The bounded medium evicted the first three hosts (LRU cap), not a bare dict.
    assert jar.cookie_header_for(canonical("http://h0.example/")) == ""
    assert jar.cookie_header_for(canonical(f"http://h{cap + 2}.example/")) == f"k=v{cap + 2}"


def test_save_to_file_is_atomic_on_failure():
    jar = CookieJar()
    jar.add_cookie_header(canonical("http://x.example/p"), "sess=abc; Path=/")
    target = os.path.join(tempfile.gettempdir(), "ck_atomic_test.json")
    with open(target, "w", encoding="utf-8") as f:
        f.write('{"old": true}')

    class Boom(Exception):
        pass

    def boom(*args, **kwargs):
        raise Boom("disk full")

    try:
        with patch("json.dumps", side_effect=boom):
            with contextlib.suppress(Boom):
                jar.export_to_file(target)
        # On failure the original target must be untouched (atomic rename only happens after a successful
        # write).
        with open(target, encoding="utf-8") as f:
            assert json.load(f) == {"old": True}
    finally:
        if os.path.exists(target):
            os.unlink(target)


def test_save_to_file_persists_and_no_temp_leftovers():
    jar = CookieJar()
    jar.add_cookie_header(canonical("http://x.example/p"), "sess=abc; Path=/")
    with cookie_file(jar, "ck_persist_test.json") as target:
        assert read_cookie_file(target) == {"x.example": {"sess": "abc"}}
    # No leftover.tmp files in the directory.
    leftovers = [n for n in os.listdir(tempfile.gettempdir()) if n.startswith(".cookiejar.")]
    assert not leftovers


def test_concurrent_cookie_writes_do_not_raise():
    jar = CookieJar()
    url = canonical("http://x.example/p")

    def worker(thread_id: int) -> None:
        for i in range(50):
            jar.add_cookie_header(url, f"k{thread_id}_{i}=v; Path=/")
            _ = jar.cookie_header_for(url)

    errors = run_thread_fanout(worker)
    assert not errors, errors


def _cookie_jar_max_hosts() -> int:
    """The jar's LRU cap, read from the module that owns it."""
    from app.transport.cookie_jar import COOKIE_JAR_MAX_HOSTS_PER_NAMESPACE

    return COOKIE_JAR_MAX_HOSTS_PER_NAMESPACE


def _jar_with_hosts(count: int):
    """A jar holding ``host0..host{count - 1}``, each with a distinct cookie.

    The LRU tests are about *which* host is evicted, so the population must be
    named and identical -- per-test loops filled different slot counts.
    """
    jar = CookieJar()
    for i in range(count):
        jar.add_cookie_header(canonical(f"http://host{i}.example/"), f"k=v{i}; Path=/")
    return jar


def test_cookie_jar_evicts_oldest_host_past_cap():
    """The jar is module-global and long-lived: distinct (namespace, host)
    slots are LRU-bounded so every crawled host cannot accumulate forever."""
    cap = _cookie_jar_max_hosts()
    jar = _jar_with_hosts(cap + 5)
    # The LRU cap keeps exactly the newest cap hosts: the five oldest are evicted.
    assert jar.cookie_header_for(canonical("http://host0.example/")) == ""
    assert jar.cookie_header_for(canonical("http://host4.example/")) == ""
    newest = cap + 4
    assert jar.cookie_header_for(canonical(f"http://host{newest}.example/")) == f"k=v{newest}"


def test_cookie_jar_read_refreshes_host_recency():
    """Reading a host's cookies marks it recently used: a read host survives
    the next eviction instead of the oldest-unread slot."""
    cap = _cookie_jar_max_hosts()
    jar = _jar_with_hosts(cap)
    assert jar.cookie_header_for(canonical("http://host0.example/")) == "k=v0"
    jar.add_cookie_header(canonical("http://fresh.example/"), "k=fresh; Path=/")
    # The freshly-read host0 survived the eviction the new host forced.
    assert jar.cookie_header_for(canonical("http://host0.example/")) == "k=v0"
    assert jar.cookie_header_for(canonical("http://fresh.example/")) == "k=fresh"


class TestCookieJarHeaderHandling:
    def test_http_cookie_invalid(self):
        # Call with an invalid cookie header - should not raise.
        global_cookie_jar().add_cookie_header(canonical("http://x.example/"), "http://example.com/")
