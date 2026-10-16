"""The connection preamble every cross-process coordination store shares.

The shared cookie jar and counters hold correct state only because it lives in
the database -- the defective process-local ``Lock`` invisible to siblings.
"""

from __future__ import annotations

import asyncio
import functools
import sqlite3
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any, ClassVar, Concatenate, ParamSpec, TypeVar

from app.storage.migrations.manifest import StoreSchema
from app.storage.sqlite_store import connect_store, process_lock, store_lock_key

__all__ = ["CoordinationStore", "atwin"]

P = ParamSpec("P")
R = TypeVar("R")


class CoordinationStore:
    """Base for a store whose shared state is the database's, not the process's.

    Subclasses declare ``schema`` (a class attribute, so a store that forgets it
    fails at construction instead of opening a connection to a neighbour's table).
    """

    schema: ClassVar[StoreSchema]

    def __init__(self, db_path: str | Path) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        # The store chokepoint owns the PRAGMAs and the migration: a store that
        # opened its own connection could run the migration twice, or drift WAL.
        self._conn: sqlite3.Connection = connect_store(db_path, schema=self.schema)
        # The same file-wide lock the migration took, not a private one: a
        # per-instance lock excludes this store from nothing.
        self._lock = process_lock(store_lock_key(db_path))

    def close(self) -> None:
        """Close the connection under the lock -- on the base, not per store, since
        a store nobody extended once silently held a connection open forever.
        """
        with self._lock:
            self._conn.close()


def atwin[Owner, **P, R](
    sync: Callable[Concatenate[Owner, P], R],
) -> Callable[Concatenate[Owner, P], Coroutine[Any, Any, R]]:
    """The async twin of a store method, declared FROM the sync method: the
    ``a``-prefixed twin runs the sync call on a worker thread so the loop never
    waits on the store lock, keeping one signature instead of a drifting copy.
    """

    @functools.wraps(sync)
    async def twin(self: Owner, /, *args: P.args, **kwargs: P.kwargs) -> R:
        # Resolved through the instance at CALL time, not the captured function:
        # a subclass override (the bus-publishing store) shadows it for the twin.
        sync_now: Callable[..., R] = getattr(self, sync.__name__)
        return await asyncio.to_thread(sync_now, *args, **kwargs)

    return twin
