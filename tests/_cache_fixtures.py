"""Shared content-cache fixtures: scoped caches and watch doubles."""

from __future__ import annotations

import contextlib
import tempfile
import threading
from collections.abc import Iterator
from typing import Any

import crawlutron.transport.content_cache as cc
from crawlutron.transport.content_cache import ContentCache


@contextlib.contextmanager
def scoped_cache(**settings) -> Iterator[ContentCache]:
    """A ContentCache over a throwaway directory, removed when the block ends.

    This owns the near-duplicated arrange; warm-restart and seeder sites stay
    out. Caps pass through--a literal cap here once drifted 8x under default.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        yield ContentCache(tmpdir, **settings)


@contextlib.contextmanager
def parked_entry_unlink(monkeypatch, cache) -> Iterator[tuple[threading.Event, threading.Event]]:
    """Park entry unlinks until released; return ``(started, release)``. A
    parked unlink proves the commit lock is free while disk I/O runs.
    """
    real_unlink = cc.Path.unlink
    started = threading.Event()
    release = threading.Event()

    def parked(self_, *args, **kwargs):
        if self_.parent == cache.cache_dir and not self_.name.startswith("."):
            started.set()
            release.wait(timeout=30)
        return real_unlink(self_, *args, **kwargs)

    monkeypatch.setattr(cc.Path, "unlink", parked)
    yield started, release


@contextlib.contextmanager
def entry_replaced_by_directory(cache, key) -> Iterator[Any]:
    """Swap an entry's file for a directory so reading it raises OSError.
    Undone on exit; a RAM-resident value serves while a disk-hit is None.
    """
    path = entry_path(cache, key)
    path.unlink()
    path.mkdir()
    try:
        yield path
    finally:
        path.rmdir()


@contextlib.contextmanager
def counting_entry_reads(monkeypatch, cache, key) -> Iterator[list[int]]:
    """Count one entry's ``read_bytes`` calls while active (non-destructive).
    A RAM hit reads 0 bytes, a disk hit 1 -- the observable told apart.
    """
    path = entry_path(cache, key)
    real_read_bytes = cc.Path.read_bytes
    count: list[int] = [0]

    def counted(self_, *args, **kwargs):
        if self_ == path:
            count[0] += 1
        return real_read_bytes(self_, *args, **kwargs)

    monkeypatch.setattr(cc.Path, "read_bytes", counted)
    yield count


def entry_path(cache, key: str):
    """The deterministic on-disk file for ``key``, from public cache_dir and
    namespace, so a test can stat, corrupt or remove a key's exact file.
    """
    import hashlib

    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return cache.cache_dir / f"{cache.namespace}.{digest}.json"


def on_disk_bytes(cache) -> int:
    """Sum of the JSON entry files on disk for this cache's namespace.

    A behavioral stand-in for the cache's internal byte accounting: the eviction
    budget must track the live on-disk footprint, so tests assert the real files.
    """
    return sum(p.stat().st_size for p in cache.cache_dir.glob(f"{cache.namespace}.*.json"))


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
