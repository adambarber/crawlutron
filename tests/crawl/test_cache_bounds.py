"""Entry bounds hold: overwrite, sharing, max entries and LRU."""

from __future__ import annotations

import json

from app.transport.content_cache import ContentCache
from tests._fetcher_factories import make_seeded_cache
from tests.crawl._cache_fixtures import _make_cache, entry_replaced_by_directory, scoped_cache


class TestContentCacheOverwrite:
    def test_content_cache_set_overwrite(self):
        with scoped_cache(default_ttl=60) as cache:
            cache.set("key", {"data": "first"})
            cache.set("key", {"data": "second"})
            assert cache.get("key") == {"data": "second"}

    def test_a_row_with_an_unparsable_expiry_is_a_miss_on_every_surface(self, tmp_path):
        """A malformed ``expires`` must not raise, and must read as a miss.

        A string expiry raised ``TypeError`` through every surface, so one bad
        row made every later read raise; asserted on both answer paths.
        """

        warm, _clock = make_seeded_cache(tmp_path, "key1")
        path = warm.path_of("key1")
        envelope = json.loads(path.read_text())
        envelope["expires"] = "not-a-timestamp"
        path.write_text(json.dumps(envelope))
        # A second cache over the same directory is the cold reader: the warm
        # one answers from its RAM mirror and never reaches the row.
        cold, _clock2 = make_seeded_cache(tmp_path, default_ttl=60)

        assert cold.get("key1") is None
        assert cold.cached_at_of("key1") is None

    def test_content_cache_prune_corrupt_entry(self, tmp_path):
        cache, clock = make_seeded_cache(tmp_path, "key1")
        # The corrupt file carries the name prune would compute, so a prune that cannot read it must skip it
        # rather than count it as removed.
        cache.path_of("badkey").write_text("not valid json")
        clock[0] = 1002.0  # advance past the TTL
        assert cache.prune() == 1  # only key1 is expired and removable

    def test_content_cache_prune_os_error(self, tmp_path):
        # An entry whose unlink raises must not abort the sweep: the other expired entries still get removed,
        # which is the property.
        cache, clock = make_seeded_cache(tmp_path, "key1", "key2")
        clock[0] = 1002.0
        with entry_replaced_by_directory(cache, "key1"):
            assert cache.prune() == 1
        assert cache.path_of("key2").exists() is False

    def test_content_cache_clear_os_error(self):
        # Same continuation contract as prune: an undeletable entry is skipped and the rest of the namespace
        # is still emptied.
        with scoped_cache(default_ttl=60) as cache:
            cache.set("key1", {"data": "1"})
            cache.set("key2", {"data": "2"})
            with entry_replaced_by_directory(cache, "key1"):
                assert cache.clear() == 1
            assert cache.path_of("key2").exists() is False


class TestSharedContentCache:
    def test_engine_shared_content_cache(self):
        from app.orchestration.engine import get_shared_content_cache

        cache = get_shared_content_cache()
        assert isinstance(cache, ContentCache)


def test_default_max_entries_is_bounded_not_none():
    """ContentCache has a bounded default max_entries."""

    with scoped_cache(default_ttl=3600) as c:
        # The default should be a positive integer, not None (which meant unbounded).
        assert c.max_entries is not None
        assert isinstance(c.max_entries, int)
        assert c.max_entries > 0


def test_max_entries_enforces_bound(tmp_path):
    """Cache never exceeds max_entries."""
    cache = _make_cache(tmp_path, max_entries=3)
    cache.set("a", 1)
    cache.set("b", 2)
    cache.set("c", 3)
    # 3 entries
    assert cache.get("a") == 1
    assert cache.get("b") == 2
    assert cache.get("c") == 3

    # Add 'd' -> one must be evicted to stay at 3
    cache.set("d", 4)
    # Total cached entries should be 3
    assert len(list(cache.cache_dir.iterdir())) == 3
    # All remaining gets should work or return None (evicted) At least one of a,b,c was evicted; d is present
    assert cache.get("d") == 4
    # Verify we didn't lose all old entries (at least 2 of a,b,c remain)
    remaining = sum(1 for k in ("a", "b", "c") if cache.get(k) is not None)
    assert remaining >= 2


def test_explicit_max_entries_zero_disables_limit():
    """Max_entries=0 means no limit (opt-out).

    Run twice -- cap of one then cap of zero -- so "nothing was evicted" reads
    next to a cache that did evict; two records catch a dropped ``> 0`` test.
    """
    with scoped_cache(default_ttl=3600, max_entries=1) as bounded:
        bounded.set("k0", 0)
        bounded.set("k1", 1)
        assert bounded.get("k0") is None, "a cap of one should not have kept two records"

    with scoped_cache(default_ttl=3600, max_entries=0) as uncapped:
        uncapped.set("k0", 0)
        uncapped.set("k1", 1)
        assert uncapped.get("k0") == 0
        assert uncapped.get("k1") == 1


def test_lru_keeps_recently_used_entries():
    """Accessing an entry protects it from next eviction."""

    with scoped_cache(default_ttl=3600, max_entries=3) as c:
        c.set("a", 1)
        c.set("b", 2)
        c.set("c", 3)
        # Touch "a" so it becomes most-recently-used.
        assert c.get("a") == 1
        # Push past the cap twice; "a" must survive both evictions.
        c.set("d", 4)
        c.set("e", 5)
        assert c.get("a") == 1
        # And the newest entries are present.
        assert c.get("e") == 5
