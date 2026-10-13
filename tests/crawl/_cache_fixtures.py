"""Shared content-cache fixtures: scoped caches and watch doubles."""

import contextlib
import tempfile
from collections.abc import Iterator
from typing import Any

import app.transport.content_cache as cc
from app.transport.content_cache import ContentCache


@contextlib.contextmanager
def scoped_cache(**settings) -> Iterator[ContentCache]:
    """A ContentCache over a throwaway directory, removed when the block ends.

    ``with tempfile.TemporaryDirectory() as d: cache = ContentCache(d,...)``
    was the arrange at a dozen sites in this file, differing only in the
    settings; owning it here means a test cannot leave a cache writing into a
    directory it meant to clean up. The directory itself is ``cache.cache_dir``
    for the two sites that need to inspect the on-disk files.

    Two kinds of site sit outside it on purpose, because the block is not
    really this shape: one opens a *second* cache over the same directory (a
    warm restart), which needs the directory rather than a cache; the other
    seeds its entry through ``make_content_cache_with_entry``, which returns
    the clock handle the test then advances.

    Settings are passed through, never restated. The byte caps in particular
    used to be written out here as literal numbers, and one of those literals
    had drifted to a value eight times smaller than the production default --
    a test that kept passing while testing a cap nothing shipped with.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        yield ContentCache(tmpdir, **settings)


@contextlib.contextmanager
def watching_entry_unlinks(monkeypatch, cache) -> Iterator[list[bool]]:
    """Record, per cache-entry unlink, whether the cache lock was held.

    Scoped to the cache directory's own entry files on purpose:
    ``atomic_write_text`` unlinks its own ``.tmp`` file *under* the lock, so an
    unfiltered watch reports the writer's temp-file cleanup as an eviction. The
    teardown is the ``monkeypatch`` fixture's, which restores the real unlink.
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

    A file swapped for a directory is the only way to make a read fail without
    patching the reader itself, and the swap has to be undone -- otherwise the
    directory the cache is writing into is left dirty for the next test.
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
    """Point the cache's ``write_data`` seam at a watcher so a test can watch,
    or fail, the cache's disk writes.

    One seam, three questions: ``calls`` records the paths that reached disk (an
    oversized entry must never transiently be one), ``on_write`` observes each
    write -- which is how the off-loop test sees which thread ran it -- and
    ``fail`` makes the write raise, either always (``True``) or only for data
    carrying a substring, so a prior entry survives to be read back. Each of
    those was its own inline closure over the same private method.
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

    It deliberately seeds nothing: what the sites that use it are after is the
    state a cold process finds on disk, and writing an entry first would warm
    the very mirror they are testing.
    """
    return ContentCache(directory, **settings)


async def _seeded_then_write_fails(cache, monkeypatch, body="old"):
    """A cache holding a persisted value, armed to fail the next write.

    Both rollback tests start from the same place: a live entry that an
    in-flight write fails against, so what is under test is what the *failed*
    write left behind. Spelling that start state in each test meant a change to
    the seeded value or to how a write is broken had to be made twice, and one
    copy could be left asserting about a state the other never created.
    """
    await cache.aset("k", {"body": body}, ttl=60)
    watch_writes(cache, fail="new")
