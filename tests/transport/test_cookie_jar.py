"""Adversarial fixes (MEDIUM): CookieJar thread-safety and atomic persistence.

The global jar is mutated concurrently; a crash during a direct ``save_to_file``
could leave a truncated jar that silently lost all persisted cookies.
"""

import contextlib
import json
import os
import re
import tempfile
from http.cookiejar import CookieJar as StdCookieJar
from unittest.mock import patch

from crawlutron.runtime import process_state
from crawlutron.transport.cookie_jar import (
    CookieJar,
    RecordingSendPolicy,
    cookie_from,
    cookie_header_request,
    global_cookie_jar,
)
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


def test_concurrent_set_and_delete_same_cookie_consistent():
    """Same (namespace, host, name) set and deleted from parallel threads,
    interleaved with reads: tombstone consistent, no resurrected cookie,
    no ``RuntimeError: dictionary changed size during iteration``."""
    jar = CookieJar()
    url = canonical("http://x.example/p")

    def worker(thread_id: int) -> None:
        for _ in range(100):
            if thread_id % 2 == 0:
                jar.add_cookie_header(url, "sess=alive; Path=/")
            else:
                jar.add_cookie_header(url, "sess=; Path=/")

    errors = run_thread_fanout(worker)
    assert not errors, errors
    # Header is live, empty-valued, or tombstoned -- never both or resurrected;
    # the guarantee is no RuntimeError from concurrent _deleted mutation.
    header = jar.cookie_header_for(url)
    assert header in ("", "sess=alive", "sess="), header


def _cookie_jar_max_hosts() -> int:
    """The jar's LRU cap, read from the module that owns it."""
    from crawlutron.transport.cookie_jar import COOKIE_JAR_MAX_HOSTS_PER_NAMESPACE

    return COOKIE_JAR_MAX_HOSTS_PER_NAMESPACE


def _jar_with_hosts(count: int):
    """A jar holding ``host0..host{count - 1}``, each with its own distinct cookie.

    Distinct names (``k0..k{count - 1}``) keep every test off the name-collision
    behaviour (#414): a same-named population made these tests pass on luck.
    """
    jar = CookieJar()
    for i in range(count):
        jar.add_cookie_header(canonical(f"http://host{i}.example/"), f"k{i}=v{i}; Path=/")
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
    assert jar.cookie_header_for(canonical(f"http://host{newest}.example/")) == f"k{newest}=v{newest}"


def test_cookie_jar_read_refreshes_host_recency():
    """Reading a host's cookies marks it recently used: a read host survives
    the next eviction instead of the oldest-unread slot."""
    cap = _cookie_jar_max_hosts()
    jar = _jar_with_hosts(cap)
    assert jar.cookie_header_for(canonical("http://host0.example/")) == "k0=v0"
    jar.add_cookie_header(canonical("http://fresh.example/"), "k=fresh; Path=/")
    # The freshly-read host0 survived the eviction the new host forced.
    assert jar.cookie_header_for(canonical("http://host0.example/")) == "k0=v0"
    assert jar.cookie_header_for(canonical("http://fresh.example/")) == "k=fresh"


class TestCookieJarHeaderHandling:
    def test_http_cookie_invalid(self):
        # Call with an invalid cookie header - should not raise.
        global_cookie_jar().add_cookie_header(canonical("http://x.example/"), "http://example.com/")


def test_reset_rebinds_a_fresh_default_jar():
    """Reset must drop the installed jar object, not just its contents.

        A test that swaps the process global for a guarded double and resets left that
    double installed for every later test in the worker -- the engine's reset rebuilds.
    """
    from crawlutron.transport import cookie_jar

    class _SentinelJar(cookie_jar.CookieJar):
        """A jar identity the reset cannot produce, so a mere clear cannot pass."""

    sentinel = _SentinelJar()
    cookie_jar.set_global_cookie_jar(sentinel)
    try:
        process_state.reset_all_process_state()
        assert cookie_jar.global_cookie_jar() is not sentinel, (
            "the registry reset left the swapped-in jar installed as the process global"
        )
        assert type(cookie_jar.global_cookie_jar()) is cookie_jar.CookieJar
    finally:
        process_state.reset_all_process_state()


def test_cookie_jar_read_refreshes_only_the_slots_it_carried():
    """A read refreshes only the slots whose cookies it carried (#414)."""
    cap = _cookie_jar_max_hosts()
    jar = CookieJar()
    for i in range(cap):
        jar.add_cookie_header(canonical(f"http://host{i}.example/"), "k=v; Path=/")
    assert jar.cookie_header_for(canonical("http://host0.example/")) == "k=v"
    jar.add_cookie_header(canonical("http://fresh.example/"), "k=fresh; Path=/")
    # Read refreshed host0 only; eviction takes LRU-oldest unread slot (host1)
    assert jar.cookie_header_for(canonical("http://host0.example/")) == "k=v"
    assert jar.cookie_header_for(canonical("http://host1.example/")) == ""
    assert jar.cookie_header_for(canonical("http://fresh.example/")) == "k=fresh"


def test_contributor_derivation_equivalence_pin():
    """Equivalence pin for #414/#418: contributors == slots whose cookies stdlib sends.
    Case 1: same-named cookies at different paths -> both contributors.
    Case 2: secure cookie on plain HTTP filtered -> only non-secure contributor; OLD fails."""
    from crawlutron.transport.cookie_jar import CookieRecord, sendable_cookie_header
    from tests._policy_factories import canonical

    # Case 1: two same-named cookies at different paths on same host
    rec1 = CookieRecord(
        value="v1", domain="example.com", path="/a", secure=False, expires=None, host_only=True
    )
    rec2 = CookieRecord(
        value="v2", domain="example.com", path="/a/b", secure=False, expires=None, host_only=True
    )
    records = [
        ("same", "example.com", rec1),
        ("same", "example.com", rec2),
    ]
    url = canonical("http://example.com/a/b/page")
    _header, contributors = sendable_cookie_header(records, url)
    assert contributors == {"example.com"}, f"case 1 failed: {contributors}"

    # Case 2: secure cookie filtered on plain HTTP + same-named non-secure
    rec_secure = CookieRecord(
        value="secure_val", domain="example.com", path="/", secure=True, expires=None, host_only=True
    )
    rec_plain = CookieRecord(
        value="plain_val", domain="example.com", path="/", secure=False, expires=None, host_only=True
    )
    records2 = [
        ("shared", "example.com", rec_secure),
        ("shared", "example.com", rec_plain),
    ]
    url2 = canonical("http://example.com/page")
    _header2, contributors2 = sendable_cookie_header(records2, url2)
    assert "shared=plain_val" in _header2, f"secure cookie leaked: {_header2}"
    assert "shared=secure_val" not in _header2, f"secure cookie in header: {_header2}"
    assert contributors2 == {"example.com"}, f"case 2 failed: {contributors2}"


def _build_jar_slots(records):
    """Build slots map by populating a stdlib CookieJar (test-only helper)."""
    policy = RecordingSendPolicy()
    jar = StdCookieJar(policy=policy)
    slots_map: dict[int, str] = {}
    items = sorted(records, key=lambda item: -len(item[2]["path"]))
    i = 0
    while i < len(items):
        name, slot, record = items[i]
        cookie = cookie_from(name, record)
        slots_map[id(cookie)] = slot
        jar.set_cookie(cookie)
        i += 1
    return policy, jar, slots_map


def _old_derive_contributors(records, url):
    """OLD derivation: contributors = all slots whose cookie name is in header."""
    policy, jar, slots_map = _build_jar_slots(records)
    request = cookie_header_request(url.raw)
    jar.add_cookie_header(request)
    header = request.get_header("Cookie") or ""
    # OLD: parse header for names, then ALL slots with those names are contributors

    header_names = set(re.findall(r"([^=;]+)=", header))
    return {slot for name, slot, _ in records if name in header_names}


def test_contributor_derivation_equivalence_pin_old_fails():
    """WALK-PAST for #418: OLD name-membership fails pin on case 2.
    Not run by default; run manually to verify walk-past.
    """
    from crawlutron.transport.cookie_jar import CookieRecord
    from tests._policy_factories import canonical

    # Case 2: secure cookie filtered on plain HTTP + same-named non-secure
    rec_secure = CookieRecord(
        value="secure_val", domain=".example.com", path="/", secure=True, expires=None, host_only=False
    )
    rec_plain = CookieRecord(
        value="plain_val", domain=".example.com", path="/", secure=False, expires=None, host_only=False
    )
    records = [
        ("shared", "secure.example.com", rec_secure),
        ("shared", "plain.example.com", rec_plain),
    ]
    url = canonical("http://sub.example.com/page")

    # OLD derivation: both slots contributors (name in header)
    old_contributors = _old_derive_contributors(records, url)
    assert old_contributors == {"secure.example.com", "plain.example.com"}, f"OLD: {old_contributors}"

    # NEW derivation: only plain cookie contributor (secure filtered)
    from crawlutron.transport.cookie_jar import sendable_cookie_header

    _header, new_contributors = sendable_cookie_header(records, url)
    assert new_contributors == {"plain.example.com"}, f"NEW: {new_contributors}"

    # OLD != NEW on case 2
    assert old_contributors != new_contributors
    assert new_contributors == {"plain.example.com"}
