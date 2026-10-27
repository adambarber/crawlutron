"""Red-first pins for #547: the frontier queue lives in its own delta table.

The ``frontier`` JSON column re-serialized the whole outstanding queue per
batch persist; the queue now appends and deletes rows by delta.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from app.storage.job_store import FrontierEntry, JobStatus
from app.storage.sqlite_store import table_columns
from tests._job_store_doubles import frontier, loaded_job, make_job_store, seed_frontier
from tests._policy_factories import canonical


def _queued_job(store, count: int):
    """A running whole-site job whose queue holds ``count`` entries."""
    job = store.create("https://example.test/root", {"limits": {"max_depth": 2}})
    seed_frontier(job, frontier([f"https://example.test/p{i}" for i in range(count)]))
    store.save(job)
    store.set_status(job.job_id, JobStatus.RUNNING, expected=JobStatus.PENDING)
    return loaded_job(store, job.job_id)


def _queue_rows(db_path, job_id: str) -> list[tuple[int, str]]:
    with sqlite3.connect(db_path) as conn:
        return [
            (row[0], row[1])
            for row in conn.execute(
                'SELECT pos, url FROM "crawl_job_frontier" WHERE job_id = ? ORDER BY pos', (job_id,)
            ).fetchall()
        ]


def _pop(job, count: int) -> None:
    """Pop the queue's leading ``count`` entries the runner does: a logged delta."""
    job.frontier_removed.extend(entry.url for entry in job.frontier[:count])
    job.frontier_mark = max(0, job.frontier_mark - count)
    del job.frontier[:count]


def test_a_batch_persist_touches_only_its_delta(tmp_path):
    """The persist writes the delta, not the queue.

    Popping a batch and admitting replacements must land exactly the new rows,
    not a rewrite of the whole queue."""
    store = make_job_store(tmp_path)
    job = _queued_job(store, 20)

    rows = _queue_rows(store.db_path, job.job_id)
    assert len(rows) == 20, f"the seed did not land 20 rows: {len(rows)}"

    # The batch cycle: pop the first 8, admit 3 replacements, save once.
    _pop(job, 8)
    job.frontier.extend(frontier([f"https://example.test/new{i}" for i in range(3)]))
    store.save(job)

    rows = _queue_rows(store.db_path, job.job_id)
    assert len(rows) == 15, f"a 20-queue minus 8 plus 3 must be 15 rows, got {len(rows)}"
    urls = [url for _pos, url in rows]
    assert urls[-3:] == [canonical(f"https://example.test/new{i}").raw for i in range(3)]
    assert canonical("https://example.test/p0").raw not in urls, "a popped entry kept its row"

    # A resave with nothing new must not re-append or reorder the queue.
    store.save(job)
    assert _queue_rows(store.db_path, job.job_id) == rows, "a resave rewrote the queue"


def test_the_job_row_stops_carrying_the_queue(tmp_path):
    """The queue is not a row column anymore.

    The column was the defect: every persist re-serialized the whole queue onto
    the row, so the row's bytes grew with the queue forever."""
    store = make_job_store(tmp_path)
    job = _queued_job(store, 50)

    with sqlite3.connect(store.db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(crawl_jobs)")}
    assert "frontier" not in columns, "the frontier column survived the split"
    assert "frontier_head" not in columns, "no queue bookkeeping belongs on the row"

    # And the table, not the row, answers for the queue's size.
    assert len(_queue_rows(store.db_path, job.job_id)) == 50


@pytest.mark.parametrize("count", [0, 1, 8])
def test_the_queue_round_trips_through_the_table_in_order(tmp_path, count):
    """A load reproduces the queue exactly, order included.

    The positions are ordinal-only, so pops leave gaps; the read must still
    land the surviving entries in queue order."""
    store = make_job_store(tmp_path)
    job = _queued_job(store, count)

    loaded = loaded_job(store, job.job_id)

    expected = [canonical(f"https://example.test/p{i}").raw for i in range(count)]
    assert [entry.url.raw for entry in loaded.frontier] == expected


def test_a_pop_then_save_then_load_resumes_where_it_left_off(tmp_path):
    """The runner's cycle over the table: pop, save, load, continue.

    A resumed process must see the queue minus the popped prefix, in order,
    with the popped entries gone -- the resumability the column gave for free."""
    store = make_job_store(tmp_path)
    job = _queued_job(store, 12)

    _pop(job, 8)
    store.save(job)

    loaded = loaded_job(store, job.job_id)
    expected = [canonical(f"https://example.test/p{i}").raw for i in range(8, 12)]
    assert [entry.url.raw for entry in loaded.frontier] == expected


def test_a_wholesale_replacement_re_lands_the_queue(tmp_path):
    """Replacing the queue deletes the rows the new list does not hold.

    A crash-seeded row can carry a queue the next writer replaces wholesale:
    the persist must express that, not leave orphan rows behind."""
    store = make_job_store(tmp_path)
    job = _queued_job(store, 6)

    seed_frontier(job, frontier(["https://example.test/fresh-a", "https://example.test/fresh-b"]))
    store.save(job)

    urls = [url for _pos, url in _queue_rows(store.db_path, job.job_id)]
    assert urls == [
        canonical("https://example.test/fresh-a").raw,
        canonical("https://example.test/fresh-b").raw,
    ], f"the replaced queue left rows behind: {urls}"


def test_a_sibling_processs_queue_entries_survive_a_growth_save(tmp_path):
    """#140 for the queue: a sibling's admitted entries are not dropped.

    A holds a queue, B appends to it, then A saves having appended one of its
    own: the reload must show both."""
    store_a = make_job_store(tmp_path)
    job = _queued_job(store_a, 10)

    store_b = make_job_store(tmp_path)
    b_job = loaded_job(store_b, job.job_id)
    b_job.frontier.extend(frontier(["https://example.test/from-b"]))
    store_b.save(b_job)

    a_job = loaded_job(store_a, job.job_id)
    a_job.frontier.extend(frontier(["https://example.test/from-a"]))
    store_a.save(a_job)

    urls = [url for _pos, url in _queue_rows(store_a.db_path, job.job_id)]
    assert canonical("https://example.test/from-b").raw in urls, "the sibling's entry was dropped"
    assert canonical("https://example.test/from-a").raw in urls
    assert len(urls) == 12


def test_deleting_a_job_takes_its_queue_with_it(tmp_path):
    """The frontier table is not an orphan store."""
    store = make_job_store(tmp_path)
    job = _queued_job(store, 5)

    assert store.delete(job.job_id) is True
    assert _queue_rows(store.db_path, job.job_id) == []


def test_a_requeued_identity_moves_to_the_end_and_keeps_its_depth(tmp_path):
    """A transient retry's requeue re-lands the identity at the queue's end.

    The retry re-admits through the write edge, so the row's new position and
    the admitted depth are the write edge's decision, never a stale row's."""
    store = make_job_store(tmp_path)
    job = _queued_job(store, 4)

    retried = job.frontier[0]
    _pop(job, 1)
    job.frontier.append(FrontierEntry(url=retried.url, depth=2))
    store.save(job)

    rows = _queue_rows(store.db_path, job.job_id)
    assert rows[-1] == (rows[-1][0], retried.url.raw)
    loaded = loaded_job(store, job.job_id)
    assert loaded.frontier[-1] == FrontierEntry(retried.url, 2)


class TestMigrationCarriesTheQueue:
    """0006 carries a legacy column's queue into the table, and back."""

    @staticmethod
    def _loaded_steps():
        """0006's (apply, rollback), loaded the identity test's way: yoyo's
        step() walks the stack for a frame holding the collector."""
        import importlib.util
        import pathlib

        from yoyo import migrations as _yoyo

        migration_path = (
            pathlib.Path(__file__).parents[2]
            / "app"
            / "storage"
            / "migrations"
            / "0006_crawl_jobs_split_frontier.py"
        )
        collector = _yoyo.StepCollector(migration=None)
        spec = importlib.util.spec_from_file_location("m0006", migration_path)
        assert spec is not None and spec.loader is not None
        m0006 = importlib.util.module_from_spec(spec)
        m0006.__dict__["__yoyo_collector__"] = collector
        spec.loader.exec_module(m0006)
        return m0006.apply_step, m0006.rollback_step

    def _rescued(self, tmp_path):
        """0006's steps, a store, and a queued job, ready for a direct step call."""
        apply_step, rollback_step = self._loaded_steps()
        store = make_job_store(tmp_path)
        return apply_step, rollback_step, store, _queued_job(store, 3)

    def test_the_carry(self, tmp_path):
        _apply, _rollback, store, job = self._rescued(tmp_path)
        with sqlite3.connect(store.db_path) as conn:
            # A database the migration is rescuing: the legacy column is back
            # (as a pre-0006 row would carry it) and the table is empty.
            conn.execute("ALTER TABLE crawl_jobs ADD COLUMN frontier TEXT NOT NULL DEFAULT '[]'")
            conn.execute(
                "UPDATE crawl_jobs SET frontier = ? WHERE job_id = ?",
                (
                    json.dumps([{"url": f"https://example.test/legacy{i}", "depth": i} for i in range(3)]),
                    job.job_id,
                ),
            )
            conn.execute('DELETE FROM "crawl_job_frontier" WHERE job_id = ?', (job.job_id,))

            _apply(conn)

            urls = [
                (row[0], row[1])
                for row in conn.execute(
                    'SELECT pos, url FROM "crawl_job_frontier" WHERE job_id = ? ORDER BY pos',
                    (job.job_id,),
                ).fetchall()
            ]
            assert [url for _pos, url in urls] == [f"https://example.test/legacy{i}" for i in range(3)], urls
            columns = {column.name for column in table_columns(conn, "crawl_jobs")}
            assert "frontier" not in columns, "the legacy column survived the carry"

    def test_the_rollback(self, tmp_path):
        _apply, rollback_step, store, job = self._rescued(tmp_path)
        with sqlite3.connect(store.db_path) as conn:
            rollback_step(conn)
            columns = {column.name for column in table_columns(conn, "crawl_jobs")}
            assert "frontier" in columns, "the rollback dropped the queue instead of restoring it"
            restored = json.loads(
                conn.execute("SELECT frontier FROM crawl_jobs WHERE job_id = ?", (job.job_id,)).fetchone()[0]
            )
            assert [entry["url"] for entry in restored] == [f"https://example.test/p{i}" for i in range(3)], (
                restored
            )
