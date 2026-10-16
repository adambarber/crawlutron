"""Shared content-cache fixtures: scoped caches and watch doubles."""

from __future__ import annotations

import contextlib
import tempfile
from collections.abc import Iterator
from typing import Any

import app.transport.content_cache as cc
from app.transport.content_cache import ContentCache


@contextlib.contextmanager
def scoped_cache(**settings) -> Iterator[ContentCache]:
    """A ContentCache over a throwaway directory, removed when the block ends.

    This owns the near-duplicated arrange; warm-restart and seeder sites stay
    out. Caps pass through--a literal cap here once drifted 8x under default.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        yield ContentCache(tmpdir, **settings)


@contextlib.contextmanager
def watching_entry_unlinks(monkeypatch, cache) -> Iterator[list[bool]]:
    """Record, per cache-entry unlink, whether the cache lock was held.

    Scoped to the cache's own entry files: the writer's ``.tmp`` cleanup
    unlinks under the lock and would otherwise read as an eviction.
    """
    real_unlink = cc.Path.unlink
    held: list[bool] = []

    def watched(self, *args, **kwargs):
        if self.parent == cache.cache_dir and not self.name.startswith("."):
            held.append(cache._lock.locked())
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(cc.Path, "unlink", watched)
    yield held


@contextlib.contextmanager
def entry_replaced_by_directory(cache, key) -> Iterator[Any]:
    """Swap an entry's file for a directory, so reading it raises OSError.

    A file-for-directory swap is the only unpatched way to fail a read, and it
    must be undone or the cache's directory is left dirty for the next test.
    """
    path = cache._path_for(key)
    path.unlink()
    path.mkdir()
    try:
        yield path
    finally:
        path.rmdir()


@contextlib.contextmanager
def watching_envelope_reads(cache) -> Iterator[list[str]]:
    """Record the keys that reach the full-envelope read, then restore the seam.

    ``cached_at_of`` is contractually a metadata question; this is what proves a
    test proved it, and the patch/restore pair was re-spelled at each site.
    """
    calls: list[str] = []
    original = cache._read_envelope

    def counting(key):
        calls.append(key)
        return original(key)

    cache._read_envelope = counting
    try:
        yield calls
    finally:
        cache._read_envelope = original


def watch_writes(cache, *, calls=None, on_write=None, fail=None):
    """Point the cache's ``write_data`` seam at a watcher to watch or fail writes.

    One seam, three questions: ``calls`` records paths to disk, ``on_write``
    observes each write, ``fail`` makes it raise (always or on a substring).
    """
    real_write = cache.write_data

    def seam(path, data):
        if calls is not None:
            calls.append(str(path))
        if on_write is not None:
            on_write(path, data)
        if fail is not None and (fail is True or fail in data):
            raise OSError("disk full")
        real_write(path, data)

    cache.write_data = seam


def _make_cache(tmp_path, **kwargs):
    kwargs.setdefault("default_ttl", 60)
    return ContentCache(str(tmp_path), **kwargs)


def reopen_cache(directory, **settings) -> ContentCache:
    """A fresh instance over a directory a previous instance wrote.

    It seeds nothing on purpose: these sites want the cold on-disk state, and
    writing an entry first would warm the mirror they are testing.
    """
    return ContentCache(directory, **settings)


async def _seeded_then_write_fails(cache, monkeypatch, body="old"):
    """A cache holding a persisted value, armed to fail the next write.

    Both rollback tests share this start: a live entry a failed in-flight
    write leaves behind. Spelling it twice let one copy drift.
    """
    await cache.aset("k", {"body": body}, ttl=60)
    watch_writes(cache, fail="new")
