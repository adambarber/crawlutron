"""Adversarial fixes (MEDIUM): CookieJar thread-safety and atomic persistence.

The shared global cookie jar is mutated by concurrent requests. Without a lock,
concurrent writes can lose updates or raise mid-iteration. And save_to_file
wrote directly to the target path, so a crash during json.dump left a
truncated/corrupted file (silently losing all persisted cookies on restart).
"""

import json
import os
import tempfile
import threading
from unittest.mock import patch

from app.crawl.strategies.http import CookieJar


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
                jar.save_to_file(target)
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
        jar.save_to_file(target)
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
    errors = []

    def worker(thread_id):
        try:
            for i in range(50):
                jar.add_cookie_header(url, f"k{thread_id}_{i}=v; Path=/")
                _ = jar.cookie_header_for(url)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors


def test_cookie_jar_evicts_oldest_host_past_cap():
    """The jar is module-global and long-lived: distinct (namespace, host)
    slots are LRU-bounded so every crawled host cannot accumulate forever."""
    from app.crawl.strategies.http import _COOKIE_JAR_MAX_HOSTS

    jar = CookieJar()
    for i in range(_COOKIE_JAR_MAX_HOSTS + 5):
        jar.add_cookie_header(f"http://host{i}.example/", f"k=v{i}; Path=/")
    assert len(jar._cookies) == _COOKIE_JAR_MAX_HOSTS
    newest = _COOKIE_JAR_MAX_HOSTS + 4
    assert jar.cookie_header_for(f"http://host{newest}.example/") == f"k=v{newest}"


def test_cookie_jar_read_refreshes_host_recency():
    """Reading a host's cookies marks it recently used: a read host survives
    the next eviction instead of the oldest-unread slot."""
    from app.crawl.strategies.http import _COOKIE_JAR_MAX_HOSTS

    jar = CookieJar()
    for i in range(_COOKIE_JAR_MAX_HOSTS):
        jar.add_cookie_header(f"http://host{i}.example/", f"k=v{i}; Path=/")
    assert jar.cookie_header_for("http://host0.example/") == "k=v0"
    jar.add_cookie_header("http://fresh.example/", "k=fresh; Path=/")
    assert jar.cookie_header_for("http://host0.example/") == "k=v0"
    assert len(jar._cookies) == _COOKIE_JAR_MAX_HOSTS
