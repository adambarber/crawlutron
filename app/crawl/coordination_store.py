"""The connection preamble every cross-process coordination store shares.

Two stores hold state that is correct only because it lives in the *database*
rather than in a process: the shared cookie jar and the shared counters. Both
were built to fix the same defect class -- a ``threading.Lock`` is invisible to
every process but the one holding it, so per-process state lost a sibling's
update or reported one worker's numbers as the fleet's -- and both module
docstrings tell that story at length. What they do about it is identical, and
the identical part is the part that must not be re-spelled: which schema the
store owns, that the PRAGMAs and the migration are the store chokepoint's, and
that the ``Lock`` serializes this process only.

Left per store, that preamble was two copies of one contract: the
``StoreSchema`` + ``connect_store`` pairing is a claim about schema
ownership, and a second copy of it is a second claim that can drift from the
first. So the pairing is here, once, and a store contributes only the schema
name it owns.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import ClassVar

from app.crawl.migrations.manifest import StoreSchema
from app.crawl.sqlite_store import connect_store, process_lock, store_lock_key

__all__ = ["CoordinationStore"]


class CoordinationStore:
    """Base for a store whose shared state is the database's, not the process's.

    Subclasses declare :attr:`schema` and nothing else about the connection. It
    is a class attribute rather than a constructor argument so a store that
    forgot it fails at construction instead of quietly opening a connection
    against a neighbour's table, and so the schema a store owns is readable off
    the class rather than off a call site.
    """

    schema: ClassVar[StoreSchema]

    def __init__(self, db_path: str | Path) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        # The store chokepoint owns the PRAGMAs and the migration; a store that
        # opened its own connection could run the migration twice or drift the
        # WAL setting, which is the whole M1 defect class.
        self._conn: sqlite3.Connection = connect_store(db_path, schema=self.schema)
        # The same file-wide lock the migration took, and not a private one: a
        # per-instance lock excludes this store from nothing else, so a write
        # here would run straight through a migration in flight on this file --
        # and through a second store instance writing to it. Cross-process
        # ordering remains SQLite's job, which is the entire reason these stores
        # exist.
        self._lock = process_lock(store_lock_key(db_path))

    def close(self) -> None:
        """Close the connection under the lock.

        On the base, not on each store: every subclass had written these three
        lines, so the close of a store nobody had extended was the one place
        still holding a connection open, and a new store could forget it.
        """
        with self._lock:
            self._conn.close()
