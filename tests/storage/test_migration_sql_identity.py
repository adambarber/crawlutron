"""Every migration's emitted SQL is pinned to its pre-helper spelling (#331).

Each migration is loaded as yoyo loads it and its steps unwrapped. The
add-column ones are pinned by what they do to a real database instead.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sqlite3
from collections.abc import Callable

import pytest
from yoyo import migrations as _yoyo

from app.storage.migrations.manifest import (
    CALLER_KEYS_SCHEMA,
    COORDINATION_COOKIES_SCHEMA,
    COORDINATION_COUNTERS_SCHEMA,
    CRAWL_JOBS_SCHEMA,
    RESULTS_SCHEMA,
    WATCHES_SCHEMA,
    create_index_sql,
    create_table_sql,
)

MIGRATIONS_ROOT = pathlib.Path(__file__).parents[2] / "app" / "storage" / "migrations"

#: A step's apply and rollback: a statement string, or a callable that runs it.
Step = str | Callable[[sqlite3.Connection], None]

# The pre-helper spelling, quoted here so the refactor to shared helpers cannot
# silently move a statement.
_EXPECTED_SQL: dict[str, list[tuple[str, str]]] = {
    "0001_crawl_jobs_initial": [(create_table_sql(CRAWL_JOBS_SCHEMA), 'DROP TABLE "crawl_jobs"')],
    "0001_results_initial": [(create_table_sql(RESULTS_SCHEMA), 'DROP TABLE "results"')],
    "0001_watches_initial": [
        (create_table_sql(WATCHES_SCHEMA), 'DROP TABLE "watches"'),
        *(
            (create_index_sql(WATCHES_SCHEMA, index), f'DROP INDEX "{index.name}"')
            for index in WATCHES_SCHEMA.indexes
        ),
    ],
    "0001_caller_keys_initial": [
        (create_table_sql(CALLER_KEYS_SCHEMA), 'DROP TABLE "caller_keys"'),
        *(
            (create_index_sql(CALLER_KEYS_SCHEMA, index), f'DROP INDEX "{index.name}"')
            for index in CALLER_KEYS_SCHEMA.indexes
        ),
    ],
    "0001_coordination_cookies_initial": [
        (create_table_sql(COORDINATION_COOKIES_SCHEMA), 'DROP TABLE "coordination_cookies"')
    ],
    "0001_coordination_counters_initial": [
        (create_table_sql(COORDINATION_COUNTERS_SCHEMA), 'DROP TABLE "coordination_counters"')
    ],
}

# The add-column migrations emit callables, so their identity is what they do
# to a real database: the column they add, spelled as the pre-helper spelling.
_ADD_COLUMN_SQL: dict[str, tuple[str, str, str]] = {
    "0002_results_add_owner": ("results", "owner", "owner TEXT NOT NULL DEFAULT ''"),
    "0002_watches_add_owner": ("watches", "owner", "owner TEXT NOT NULL DEFAULT ''"),
    "0004_crawl_jobs_add_owner": ("crawl_jobs", "owner", "owner TEXT NOT NULL DEFAULT ''"),
    "0005_crawl_jobs_add_deleted_at": ("crawl_jobs", "deleted_at", "deleted_at REAL"),
}

#: The table each add-column migration's target table belongs to.
_BASE_TABLE_SQL: dict[str, str] = {
    "results": create_table_sql(RESULTS_SCHEMA),
    "watches": create_table_sql(WATCHES_SCHEMA),
    "crawl_jobs": create_table_sql(CRAWL_JOBS_SCHEMA),
}

#: Migrations this module does not pin, and why: the shared helpers never
#: touched them, and each carries bespoke data movement.
_UNPINNED: dict[str, str] = {
    "0002_crawl_jobs_drop_canonicalized": "hand-written drop with a bespoke rollback",
    "0003_crawl_jobs_split_accumulators": "carries history across into companion tables",
}


def _load_steps(migration_id: str) -> list[tuple[Step, Step]]:
    """Each step's (apply, rollback), as yoyo collects it: a statement string
    for the table migrations, a callable for the add-column ones."""
    path = MIGRATIONS_ROOT / f"{migration_id}.py"
    collector = _yoyo.StepCollector(migration=None)
    spec = importlib.util.spec_from_file_location(migration_id, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # yoyo's step() walks the call stack for a frame holding __yoyo_collector__,
    # so setting it on the module's globals is what collects the steps.
    module.__dict__["__yoyo_collector__"] = collector
    spec.loader.exec_module(module)
    pairs: list[tuple[Step, Step]] = []
    for do_add in list(collector.steps):
        wrapper = do_add(True)
        pairs.append((wrapper.step._apply, wrapper.step._rollback))
    return pairs


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()}


@pytest.mark.parametrize("migration_id", sorted(_EXPECTED_SQL))
def test_migration_emitted_sql_is_unchanged(migration_id: str) -> None:
    assert _load_steps(migration_id) == _EXPECTED_SQL[migration_id], f"{migration_id} SQL changed"


@pytest.mark.parametrize("migration_id", sorted(_ADD_COLUMN_SQL))
def test_add_column_migration_adds_exactly_its_column(migration_id: str) -> None:
    """The column it adds, spelled as the pre-helper spelling was.

    A legacy database that already carries the column no-ops the upgrade, and
    the rollback drops the column only when the migration added it."""
    table, column, column_ddl = _ADD_COLUMN_SQL[migration_id]
    apply_step, rollback_step = _load_steps(migration_id)[0]
    assert callable(apply_step) and callable(rollback_step), (
        f"{migration_id} emits a statement, not a callable"
    )

    conn = sqlite3.connect(":memory:")
    conn.execute(_BASE_TABLE_SQL[table])
    # The manifest schemas describe the FINAL table, so the column the
    # migration adds is already there: drop it to reach the pre-migration state.
    conn.execute(f'ALTER TABLE "{table}" DROP COLUMN "{column}"')
    assert column not in _columns(conn, table), f"{migration_id} added a column the table already had"

    apply_step(conn)
    assert column in _columns(conn, table), f"{migration_id} did not add {column}"

    # The legacy path: a database already carrying the column is a no-op.
    apply_step(conn)
    assert column in _columns(conn, table), f"{migration_id} dropped {column} on a second apply"

    rollback_step(conn)
    assert column not in _columns(conn, table), f"{migration_id} left {column} behind"
    rollback_step(conn)
    conn.close()


def test_every_migration_is_pinned() -> None:
    """No migration may arrive unpinned.

    A migration the pin tables do not name is a migration whose shape nothing
    holds, and the next refactor could move it without anything noticing."""
    on_disk = {path.stem for path in MIGRATIONS_ROOT.glob("0*.py")}
    pinned = set(_EXPECTED_SQL) | set(_ADD_COLUMN_SQL) | set(_UNPINNED)
    assert on_disk == pinned, (
        f"migrations not pinned by this module: {sorted(on_disk - pinned)}; "
        f"pinned but absent: {sorted(pinned - on_disk)}"
    )
