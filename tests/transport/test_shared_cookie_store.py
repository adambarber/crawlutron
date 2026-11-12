"""The cookie jar's storage must be shared, not per-process.

``save_to_file`` is atomic against truncation but not lost update; sharing
must merge in a transaction, not last-writer-wins.
"""

import pytest

from crawlutron.storage.coordination_cookies import SharedCookieStore
from crawlutron.transport.cookie_jar import CookieJar
from tests._policy_factories import canonical
from tests._thread_fanout import run_thread_fanout


@pytest.fixture
def store_path(tmp_path):
    return tmp_path / "coordination.db"


def _worker_jar(store_path) -> CookieJar:
    """One worker's jar over the shared store.

    The unit here is a pair of independently-built jars over one file -- the
    same shape a second process would have.
    """
    return CookieJar(SharedCookieStore(store_path))


def _two_workers(store_path):
    """Two independent jars over one shared store: the multi-worker shape.

    Every property here is about a second worker observing a first's write,
    so the pair is setup, not per-test.
    """
    return _worker_jar(store_path), _worker_jar(store_path)


def test_a_sibling_worker_s_cookie_is_visible(store_path):
    """The property a per-process jar cannot provide."""
    first, second = _two_workers(store_path)

    first.add_cookie_header(canonical("http://example.com/"), "session=abc; Path=/")

    assert second.cookie_header_for(canonical("http://example.com/")) == "session=abc"


def test_concurrent_writes_to_one_host_merge_rather_than_clobber(store_path):
    """The lost update, stated as a test.

    A last-writer-wins write would leave only the second cookie, so this fails
    for the same reason the real shutdown did.
    """
    first, second = _two_workers(store_path)

    first.add_cookie_header(canonical("http://example.com/"), "session=abc; Path=/")
    second.add_cookie_header(canonical("http://example.com/"), "pref=dark; Path=/")

    header = first.cookie_header_for(canonical("http://example.com/"))
    assert "session=abc" in header
    assert "pref=dark" in header


def test_namespaces_stay_isolated_in_shared_storage(store_path):
    """Sharing the store must not become sharing cookies.

    The jar's namespace is what keeps concurrent tenants from reading each
    other's cookies, so a store-wide table must key on it, not on host alone.
    """
    jar = _worker_jar(store_path)

    jar.add_cookie_header(canonical("http://example.com/"), "a=1; Path=/", namespace="tenant-a")
    jar.add_cookie_header(canonical("http://example.com/"), "b=2; Path=/", namespace="tenant-b")

    assert jar.cookie_header_for(canonical("http://example.com/"), namespace="tenant-a") == "a=1"
    assert jar.cookie_header_for(canonical("http://example.com/"), namespace="tenant-b") == "b=2"
    # The legacy shared namespace must not see either tenant's cookies.
    assert jar.cookie_header_for(canonical("http://example.com/")) == ""


def test_clear_in_one_worker_is_seen_by_another(store_path):
    """A clear that only reached its own process would replay revoked cookies."""
    first, second = _two_workers(store_path)
    first.add_cookie_header(canonical("http://example.com/"), "session=abc; Path=/")

    first.clear()

    assert second.cookie_header_for(canonical("http://example.com/")) == ""


def test_export_captures_what_a_sibling_wrote(store_path, tmp_path):
    """Shutdown export must not discard a sibling's cookies.

    This is the exact shape of the original data loss: one process exits with
    the whole file to itself and drops everything the others held.
    """
    first, second = _two_workers(store_path)
    first.add_cookie_header(canonical("http://example.com/"), "a=1; Path=/")
    second.add_cookie_header(canonical("http://other.com/"), "b=2; Path=/")

    target = tmp_path / "export.json"
    first.export_to_file(str(target))

    assert CookieJar.import_from_file(str(target)).cookie_header_for(canonical("http://other.com/")) == "b=2"


def test_a_worker_holding_no_store_keeps_the_in_memory_behavior():
    """The jar must still be constructible with no store at all.

    Two tests build ``CookieJar()`` directly, and a single-process operator
    should not need a database: memory-only is a valid configuration.
    """
    jar = CookieJar()

    jar.add_cookie_header(canonical("http://example.com/"), "session=abc; Path=/")

    assert jar.cookie_header_for(canonical("http://example.com/")) == "session=abc"


def test_store_rejects_a_hostile_namespace_as_data(tmp_path):
    """Namespaces are caller-supplied, so they are bound parameters."""
    store = SharedCookieStore(tmp_path / "coordination.db")
    hostile = "x'); DROP TABLE coordination_cookies; --"

    store.merge(hostile, "example.com", {"a": "1"})

    assert store.load(hostile, "example.com") == {"a": "1"}
    assert store.load("", "example.com") == {}


def test_concurrent_merge_touch_load_same_slot_union(store_path):
    """Concurrent merge/touch/load on one SharedCookieStore, same slot: the final slot is the union of every
    concurrent write, and no thread hits "cannot start a transaction within a transaction" on the shared
    connection.
    """
    store = SharedCookieStore(store_path)
    ns, host = "tenant-a", "example.com"

    def worker(thread_id: int) -> None:
        for _ in range(50):
            store.merge(ns, host, {f"k{thread_id}": f"v{thread_id}"})
            store.touch(ns, host)
            _ = store.load(ns, host)

    errors = run_thread_fanout(worker)
    assert not errors, errors

    slot = store.load(ns, host)
    expected = {f"k{tid}": f"v{tid}" for tid in range(8)}
    assert slot == expected, f"slot is not the union: {slot!r} != {expected!r}"
