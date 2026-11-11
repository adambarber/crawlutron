"""The connection preamble every cross-process coordination store shares.

The shared cookie jar and counters hold correct state only because it lives in
the database -- the defective process-local ``Lock`` invisible to siblings.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import sqlite3
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import ClassVar, Concatenate, ParamSpec, TypeVar

from crawlutron.storage.migrations.manifest import StoreSchema
from crawlutron.storage.sqlite_store import bind_conn_lifecycle, connect_store, process_lock, store_lock_key

__all__ = ["CoordinationStore", "atwin"]

P = ParamSpec("P")
R = TypeVar("R")

# Bounded dedicated pool for blocking SQLite store calls. The store lock (one RLock
# per db file) already serializes in-process store access, so a modest bound is
# enough: the point is to keep store saturation from starving the default
# ``asyncio.to_thread`` executor used by extraction and other crawl work.
_STORE_WORKERS = 8
_STORE_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=_STORE_WORKERS, thread_name_prefix="coordination-store"
)


class CoordinationStore:
    """Base for a store whose shared state is the database's, not the process's.

    Subclasses declare ``schema`` (a class attribute, so a store that forgets it
    fails at construction instead of opening a connection to a neighbour's table).
    """

    schema: ClassVar[StoreSchema]
    _conn: sqlite3.Connection

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        # The store chokepoint owns the PRAGMAs and the migration: a store that
        # opened its own connection could run the migration twice, or drift WAL.
        self._conn, memory_name = connect_store(db_path, schema=self.schema)
        # The same lock the migration took, not a private one: a per-instance lock
        # excludes this store from nothing. On the memory path that is its own name.
        self.lock_key = store_lock_key(db_path, memory_name)
        self._lock = process_lock(self.lock_key)
        # A store whose last reference dies closes its connection at that GC
        # point — never at interpreter-exit finalization (see bind_conn_lifecycle).
        bind_conn_lifecycle(self)

    def close(self) -> None:
        """Close the connection under the lock -- on the base, not per store, since
        a store nobody extended once silently held a connection open forever.
        """
        with self._lock:
            self._conn.close()


def atwin[Owner, **P, R](
    sync: Callable[Concatenate[Owner, P], R],
) -> Callable[Concatenate[Owner, P], Awaitable[R]]:
    """The async twin of a store method, declared FROM the sync method: the
    ``a``-prefixed twin runs the sync call on a worker thread so the loop never
    waits on the store lock, keeping one signature instead of a drifting copy.
    """

    @functools.wraps(sync)
    async def twin(self: Owner, /, *args: P.args, **kwargs: P.kwargs) -> R:
        # Resolved through the instance at CALL time, not the captured function:
        # a subclass override (the bus-publishing store) shadows it for the twin.
        sync_now: Callable[..., R] = getattr(self, sync.__name__)
        # Runs on the dedicated store executor, never the default asyncio pool:
        # store saturation can never starve extraction and the rest of the loop.
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_STORE_EXECUTOR, functools.partial(sync_now, *args, **kwargs))

    return twin
