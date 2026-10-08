"""How things persist: the storage layer.

Every SQLite table this service owns is declared, migrated and read from here. One
file holds them all: `sqlite_store` owns the connection, the process lock and the
journal settings, and `coordination_store` is the base every store on top of it
shares. The stores are the records a crawl produces -- `job_store` for the job
and frontier, `result_store` for finished per-URL results, `watch_store` for
registered watches -- plus the two coordination tables that outlive a single
request, cookies and counters.

`migrations` is the only place a schema may change. A store declares its schema
through `StoreSchema`; `migrations/manifest.py` holds the DDL and
`migrations/runner.py` applies it under the same lock the store takes, so an
out-of-band change cannot arrive between a process's read of the schema and its
first query.

Handles are minted here too (`ids`), so one primitive mints every id rather than
each caller reaching for uuid4 and inventing a second format.

`app/orchestration` decides what to store; this layer only knows how to keep it.
"""

from __future__ import annotations
