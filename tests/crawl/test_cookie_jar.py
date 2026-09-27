"""Adversarial fixes (MEDIUM): CookieJar thread-safety and atomic persistence.

The shared global cookie jar is mutated by concurrent requests. Without a lock,
concurrent writes can lose updates or raise mid-iteration. And save_to_file
wrote directly to the target path, so a crash during json.dump left a
truncated/corrupted file (silently losing all persisted cookies on restart).
"""

import json
import os
import tempfile
from unittest.mock import patch

from _thread_fanout import run_thread_fanout

from app.crawl.strategies.cookie_jar import CookieJar


def test_cookie_jar_is_locked():
    # Concurrent access must be guarded by a lock.
    assert hasattr(CookieJar(), "_lock")


def test_save_to_file_is_atomic_on_failure():
    jar = CookieJar()
    jar.add_cookie_header("http://x.example/p", "sess=abc; Path=/")
    target = os.path.join(tempfile.gettempdir(), "ck_atomic_test.json")
    with open(target, "w", encoding="utf-8") as f:
        f.write('{"old": true}')

    class Boom(Exception):
        pass

    def boom(*args, **kwargs):
        raise Boom("disk full")

    try:
        with patch("json.dumps", side_effect=boom):
            try:
                jar.export_to_file(target)
            except Boom:
                pass
        # On failure the original target must be untouched (atomic rename only
        # happens after a successful write).
        with open(target, encoding="utf-8") as f:
            assert json.load(f) == {"old": True}
    finally:
        if os.path.exists(target):
            os.unlink(target)


def test_save_to_file_persists_and_no_temp_leftovers():
    jar = CookieJar()
    jar.add_cookie_header("http://x.example/p", "sess=abc; Path=/")
    target = os.path.join(tempfile.gettempdir(), "ck_persist_test.json")
    if os.path.exists(target):
        os.unlink(target)
    try:
        jar.export_to_file(target)
        with open(target, encoding="utf-8") as f:
            data = json.load(f)
        assert data == {"x.example": {"sess": "abc"}}
        # No leftover .tmp files in the directory.
        leftovers = [n for n in os.listdir(tempfile.gettempdir()) if n.startswith(".cookiejar.")]
        assert not leftovers
    finally:
        if os.path.exists(target):
            os.unlink(target)


def test_concurrent_cookie_writes_do_not_raise():
    jar = CookieJar()
    url = "http://x.example/p"

    def worker(thread_id: int) -> None:
        for i in range(50):
            jar.add_cookie_header(url, f"k{thread_id}_{i}=v; Path=/")
            _ = jar.cookie_header_for(url)

    errors = run_thread_fanout(worker)
    assert not errors, errors


def test_cookie_jar_evicts_oldest_host_past_cap():
    """The jar is module-global and long-lived: distinct (namespace, host)
    slots are LRU-bounded so every crawled host cannot accumulate forever."""
    from app.crawl.strategies.cookie_jar import _COOKIE_JAR_MAX_HOSTS

    jar = CookieJar()
    for i in range(_COOKIE_JAR_MAX_HOSTS + 5):
        jar.add_cookie_header(f"http://host{i}.example/", f"k=v{i}; Path=/")
    assert len(jar._cookies) == _COOKIE_JAR_MAX_HOSTS
    newest = _COOKIE_JAR_MAX_HOSTS + 4
    assert jar.cookie_header_for(f"http://host{newest}.example/") == f"k=v{newest}"


def test_cookie_jar_read_refreshes_host_recency():
    """Reading a host's cookies marks it recently used: a read host survives
    the next eviction instead of the oldest-unread slot."""
    from app.crawl.strategies.cookie_jar import _COOKIE_JAR_MAX_HOSTS

    jar = CookieJar()
    for i in range(_COOKIE_JAR_MAX_HOSTS):
        jar.add_cookie_header(f"http://host{i}.example/", f"k=v{i}; Path=/")
    assert jar.cookie_header_for("http://host0.example/") == "k=v0"
    jar.add_cookie_header("http://fresh.example/", "k=fresh; Path=/")
    assert jar.cookie_header_for("http://host0.example/") == "k=v0"
    assert len(jar._cookies) == _COOKIE_JAR_MAX_HOSTS


class TestCookieJarHeaderHandling:
    def test_http_cookie_invalid(self):
        from app.crawl.strategies.cookie_jar import _GLOBAL_COOKIE_JAR

        # Call with invalid cookie header - should not raise
        _GLOBAL_COOKIE_JAR.add_cookie_header({}, "http://example.com/")


class _RecordingResponse:
    """Minimal ``httpx.Response`` stand-in carrying a Set-Cookie header.

    ``_NoStoreCookieJar.extract_cookies`` ignores its arguments entirely, but
    the fakes stay realistic so the test would still exercise the storing path
    if the override were ever removed.
    """

    def __init__(self) -> None:
        self.headers = {"Set-Cookie": "session=abc; Path=/"}


class _RecordingRequest:
    def __init__(self) -> None:
        self.url = "http://example.com/"


def test_no_store_cookie_jar_inherits_the_stdlib_jar_not_the_storing_one():
    """P2-B: ``_NoStoreCookieJar`` must subclass ``http.cookiejar.CookieJar``.

    The module defines its own storing ``CookieJar`` further down, and the
    inert jar's base is resolved at class-creation time. Binding it through the
    aliased stdlib import makes the base independent of source ordering -- the
    shadowing was load-bearing: moving the inert class below the storing one
    would silently rebind its base and invert its purpose.
    """
    import http.cookiejar

    from app.crawl.strategies import http_client_pool as pool_mod
    from app.crawl.strategies.cookie_jar import CookieJar

    assert issubclass(pool_mod._NoStoreCookieJar, http.cookiejar.CookieJar)
    # The project's own jar STORES cookies; the inert jar must not be one.
    assert not issubclass(pool_mod._NoStoreCookieJar, CookieJar)


def test_no_store_cookie_jar_never_stores_a_set_cookie():
    """The inert jar must stay empty after a response carries a Set-Cookie."""
    from app.crawl.strategies.http_client_pool import _NoStoreCookieJar

    jar = _NoStoreCookieJar()
    assert jar.extract_cookies(_RecordingResponse(), _RecordingRequest()) is None
    assert not list(jar), "the inert jar stored a cookie it must never replay"
