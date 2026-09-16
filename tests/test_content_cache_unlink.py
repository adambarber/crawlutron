"""Content-cache eviction: disk unlinks stay off the lock, and a key re-set
between its eviction and the deferred unlink keeps its live file."""

from _testkit import _temp_cache


class TestContentCacheUnlinkDiscipline:
    def test_eviction_unlinks_outside_the_lock(self, monkeypatch, tmp_path):
        """Eviction mutates the index/byte accounting under the lock and unlinks
        the doomed files only after releasing it: the event-loop thread
        contends on this lock, so disk I/O must never hold it."""
        import app.crawl.content_cache as cc_mod

        cache = cc_mod.ContentCache(tmp_path, max_entries=2, max_total_bytes=None, default_ttl=0)
        real_unlink = cc_mod.Path.unlink

        def _unlink_unlocked(self, *args, **kwargs):
            # Only cache-entry files matter; atomic_write_text unlinks its own
            # .tmp file under the lock and that is not disk eviction I/O.
            if self.parent == cache.cache_dir and not self.name.startswith("."):
                assert not cache._lock.locked(), "unlink ran while holding the cache lock"
            return real_unlink(self, *args, **kwargs)

        monkeypatch.setattr(cc_mod.Path, "unlink", _unlink_unlocked)
        # Over-cap by one: the oldest entry is evicted and its file unlinked.
        cache.set(cache.make_key("a"), {"v": 1})
        cache.set(cache.make_key("b"), {"v": 2})
        cache.set(cache.make_key("c"), {"v": 3})

        assert cache.get(cache.make_key("a")) is None
        assert cache.get(cache.make_key("c")) == {"v": 3}

    def test_expiry_unlinks_outside_the_lock(self, monkeypatch, tmp_path):
        """The TTL-expiry path in _read_envelope unlinks outside the lock too."""
        import app.crawl.content_cache as cc_mod

        cache, clock_vals = _temp_cache(ttl=1)
        real_unlink = cc_mod.Path.unlink
        seen = {"locked": False}

        def _unlink_unlocked(self, *args, **kwargs):
            if self.parent == cache.cache_dir and not self.name.startswith("."):
                if cache._lock.locked():
                    seen["locked"] = True
            return real_unlink(self, *args, **kwargs)

        monkeypatch.setattr(cc_mod.Path, "unlink", _unlink_unlocked)
        cache.set("key", {"data": "value"})
        clock_vals[0] = 1002.0
        assert cache.get("key") is None
        assert not seen["locked"], "expiry unlink ran while holding the cache lock"

    def test_re_set_key_survives_its_stale_eviction_unlink(self, tmp_path):
        """A key re-set after its eviction reuses the same deterministic path.

        The deferred unlink must spare a file that has become live again,
        otherwise the fresh write is destroyed and only a self-healing cache
        miss hides the loss.
        """
        import app.crawl.content_cache as cc_mod

        cache = cc_mod.ContentCache(tmp_path, max_entries=1, max_total_bytes=None, default_ttl=0)
        key = cache.make_key("hot")
        cache.set(key, {"v": 1})

        # Evict exactly as _enforce_limits does, but leave the unlink pending
        # so the re-registration can land inside the deferral window.
        doomed: list[tuple[str, cc_mod.Path]] = []
        with cache._lock:
            cache._drop_locked(key, doomed)

        # The key is written again to the same deterministic path.
        cache.set(key, {"v": "fresh"})
        cache._unlink_doomed(doomed)

        assert cache.get(key) == {"v": "fresh"}
