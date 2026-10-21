"""Every migration's emitted SQL is pinned to its pre-helper spelling (#331).

Each migration module is loaded as yoyo loads it, its steps unwrapped, and the
statements compared against the pre-helper SQL.
"""

from __future__ import annotations

import importlib.util
import pathlib

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


def _load_steps(migration_id: str) -> list[tuple[str, str]]:
    """(apply, rollback) SQL per step, exactly as yoyo collects it."""
    path = MIGRATIONS_ROOT / f"{migration_id}.py"
    collector = _yoyo.StepCollector(migration=None)
    spec = importlib.util.spec_from_file_location(migration_id, path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    module.step = collector.add_step
    module.__yoyo_collector__ = collector
    spec.loader.exec_module(module)
    pairs: list[tuple[str, str]] = []
    for do_add in list(collector.steps):
        wrapper = do_add(True)
        pairs.append((wrapper.step._apply, wrapper.step._rollback))
    return pairs


@pytest.mark.parametrize("migration_id", sorted(_EXPECTED_SQL))
def test_migration_emitted_sql_is_unchanged(migration_id: str) -> None:
    assert _load_steps(migration_id) == _EXPECTED_SQL[migration_id], f"{migration_id} SQL changed"
