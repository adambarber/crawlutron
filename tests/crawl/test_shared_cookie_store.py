"""The cookie jar's storage must be shared, not per-process.

`CookieJar.save_to_file` serialises the whole in-memory jar and replaces the
target through the atomic-write chokepoint. Atomicity prevents a *truncated*
file; it does nothing about a *lost update*, because the update is
read-nothing-then-replace-everything. Each worker loads the jar at startup and
saves it at shutdown, so with N workers shutdown is last-writer-wins: the last
worker to exit discards every cookie the others collected, and the deployment
still reports healthy. That is the same defect shape as the metrics store and
the spill sweep -- a fact held in a structure only its own process can see.

Sharing has to be additive, not replacement. Two workers crawling the same host
set different cookies, and a last-writer-wins write loses whichever landed
first, so the store merges within a transaction rather than overwriting.

The JSON file stays as import/export. It is a useful operator tool and its
format is pinned by tests; it just stops being the live persistence mechanism.
"""

import pytest

from app.crawl.coordination_cookies import SharedCookieStore
from app.crawl.strategies.cookie_jar import CookieJar


@pytest.fixture
def store_path(tmp_path):
    return tmp_path / "coordination.db"


def test_a_sibling_worker_s_cookie_is_visible(store_path):
    """The property a per-process jar cannot provide."""
    first = CookieJar(SharedCookieStore(store_path))
    second = CookieJar(SharedCookieStore(store_path))

    first.add_cookie_header("http://example.com/", "session=abc; Path=/")

    assert second.cookie_header_for("http://example.com/") == "session=abc"


def test_concurrent_writes_to_one_host_merge_rather_than_clobber(store_path):
    """The lost update, stated as a test.

    A last-writer-wins write would leave only the second cookie, so this fails
    for the same reason the real shutdown did.
    """
    first = CookieJar(SharedCookieStore(store_path))
    second = CookieJar(SharedCookieStore(store_path))

    first.add_cookie_header("http://example.com/", "session=abc; Path=/")
    second.add_cookie_header("http://example.com/", "pref=dark; Path=/")

    header = first.cookie_header_for("http://example.com/")
    assert "session=abc" in header
    assert "pref=dark" in header


def test_namespaces_stay_isolated_in_shared_storage(store_path):
    """Sharing the store must not become sharing cookies.

    The jar's namespace is what keeps concurrent tenants from reading each
    other's cookies, so a store-wide table must key on it, not on host alone.
    """
    jar = CookieJar(SharedCookieStore(store_path))

    jar.add_cookie_header("http://example.com/", "a=1; Path=/", namespace="tenant-a")
    jar.add_cookie_header("http://example.com/", "b=2; Path=/", namespace="tenant-b")

    assert jar.cookie_header_for("http://example.com/", namespace="tenant-a") == "a=1"
    assert jar.cookie_header_for("http://example.com/", namespace="tenant-b") == "b=2"
    # The legacy shared namespace must not see either tenant's cookies.
    assert jar.cookie_header_for("http://example.com/") == ""


def test_clear_in_one_worker_is_seen_by_another(store_path):
    """A clear that only reached its own process would replay revoked cookies."""
    first = CookieJar(SharedCookieStore(store_path))
    second = CookieJar(SharedCookieStore(store_path))
    first.add_cookie_header("http://example.com/", "session=abc; Path=/")

    first.clear()

    assert second.cookie_header_for("http://example.com/") == ""


def test_export_captures_what_a_sibling_wrote(store_path, tmp_path):
    """Shutdown export must not discard a sibling's cookies.

    This is the exact shape of the original data loss: one process exits with
    the whole file to itself and drops everything the others held.
    """
    first = CookieJar(SharedCookieStore(store_path))
    second = CookieJar(SharedCookieStore(store_path))
    first.add_cookie_header("http://example.com/", "a=1; Path=/")
    second.add_cookie_header("http://other.com/", "b=2; Path=/")

    target = tmp_path / "export.json"
    first.export_to_file(str(target))

    assert CookieJar.import_from_file(str(target)).cookie_header_for("http://other.com/") == "b=2"


def test_a_worker_holding_no_store_keeps_the_in_memory_behavior():
    """The jar must still be constructible with no store at all.

    Two tests construct ``CookieJar()`` directly, and an operator debugging a
    single-process run should not need a database. Memory-only is a valid
    configuration, not a degraded one.
    """
    jar = CookieJar()

    jar.add_cookie_header("http://example.com/", "session=abc; Path=/")

    assert jar.cookie_header_for("http://example.com/") == "session=abc"


def test_store_rejects_a_hostile_namespace_as_data(tmp_path):
    """Namespaces are caller-supplied, so they are bound parameters."""
    store = SharedCookieStore(tmp_path / "coordination.db")
    hostile = "x'); DROP TABLE coordination_cookies; --"

    store.merge(hostile, "example.com", {"a": "1"})

    assert store.get(hostile, "example.com") == {"a": "1"}
    assert store.get("", "example.com") == {}
