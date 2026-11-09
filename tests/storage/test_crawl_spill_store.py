"""CrawlSpillStore: SQLite spill rows replace the file spill's contract.

The engine's spill shape -- incremental per-result write, ordered read-back,
age-based reclamation -- with the database owning durability. Same behavior the
file spill pinned, one mechanism fewer.
"""

from __future__ import annotations

import json
import sqlite3

from crawlutron.storage.result_store import CrawlResultStore, CrawlSpillStore, PayloadTooLargeError
from tests._policy_factories import UNRESTRICTED_HOST_POLICY
from tests._result_builders import engine_result


def make_spill_store(tmp_path) -> CrawlSpillStore:
    return CrawlSpillStore(tmp_path / "spills.db")


def _row_count(tmp_path) -> int:
    with sqlite3.connect(tmp_path / "spills.db") as conn:
        return conn.execute("SELECT COUNT(*) FROM crawl_spills").fetchone()[0]


def _backdated_store(tmp_path, now: float) -> CrawlSpillStore:
    """A store whose clock is frozen at ``now`` -- GC arithmetic reads off it."""
    return CrawlSpillStore(tmp_path / "spills.db", clock=lambda: now)


def test_each_result_lands_as_one_row_before_the_crawl_finishes(tmp_path):
    """Incremental write: one row per result, committed per write, so a mid-crawl
    reader sees the rows already written."""
    store = make_spill_store(tmp_path)
    try:
        store.put("r1", 0, engine_result("https://spill.test/0", "rendered"))
        assert _row_count(tmp_path) == 1, "the first result must be durable before the batch finishes"
    finally:
        store.close()


def test_shuffled_writes_reassemble_in_submission_order(tmp_path):
    store = make_spill_store(tmp_path)
    try:
        for i in (5, 0, 3, 1, 4, 2):
            store.put("r1", i, engine_result(f"https://spill.test/{i}", "rendered"))
        out = store.load_ordered("r1", policy=UNRESTRICTED_HOST_POLICY)
        assert [r.original_url for r in out] == [f"https://spill.test/{i}" for i in range(6)]
    finally:
        store.close()


def test_one_crawls_rows_never_leak_into_another(tmp_path):
    store = make_spill_store(tmp_path)
    try:
        store.put("r1", 0, engine_result("https://spill.test/own", "rendered"))
        store.put("r2", 0, engine_result("https://spill.test/other", "rendered"))
        out = store.load_ordered("r1", policy=UNRESTRICTED_HOST_POLICY)
        assert [r.original_url for r in out] == ["https://spill.test/own"]
    finally:
        store.close()


def test_a_payload_past_the_cap_is_refused_before_persistence(tmp_path):
    store = CrawlSpillStore(tmp_path / "spills.db", max_payload_bytes=1)
    try:
        try:
            store.put("r1", 0, engine_result("https://spill.test/0", "rendered"))
        except PayloadTooLargeError:
            pass
        else:
            raise AssertionError("an over-cap payload persisted silently")
    finally:
        store.close()


def _backdate_all_rows(tmp_path, created_at: float) -> None:
    with sqlite3.connect(tmp_path / "spills.db") as conn:
        conn.execute("UPDATE crawl_spills SET created_at = ?", (created_at,))
        conn.commit()


def test_a_backdated_row_of_a_live_request_survives_the_sweep(tmp_path):
    """Age alone is not permission: a crawl slower than the GC age keeps its early
    rows until its reassembly releases them -- the file spill's ownership rule,
    re-derived as the store's live-writer fence."""
    now = 1000.0
    store = _backdated_store(tmp_path, now)
    try:
        store.put("r-live", 0, engine_result("https://spill.test/0", "rendered"))
        _backdate_all_rows(tmp_path, now - 7200)
        assert store.gc_stale_spills(3600.0) == 0, "the sweep reclaimed a live crawl's rows"
        out = store.load_ordered("r-live", policy=UNRESTRICTED_HOST_POLICY)
        assert [r.original_url for r in out] == ["https://spill.test/0"]
    finally:
        store.close()


def test_a_released_requests_rows_are_reclaimed_by_age(tmp_path):
    """The fence is the reassembly's release: once the crawl has served its rows
    they are garbage like any other aged row, not held forever."""
    now = 1000.0
    store = _backdated_store(tmp_path, now)
    try:
        store.put("r-done", 0, engine_result("https://spill.test/0", "rendered"))
        _backdate_all_rows(tmp_path, now - 7200)
        store.release_spill("r-done")
        assert store.gc_stale_spills(3600.0) == 1
        assert _row_count(tmp_path) == 0
    finally:
        store.close()


def test_gc_deletes_only_rows_older_than_the_age(tmp_path):
    """Age is the reclamation rule for a RELEASED request; a row's age comes from
    the store's own clock. A live request's rows are fenced (the probe above)."""
    now = 1000.0
    store = _backdated_store(tmp_path, now)
    try:
        store.put("r1", 0, engine_result("https://spill.test/0", "rendered"))
        with sqlite3.connect(tmp_path / "spills.db") as conn:
            conn.execute("UPDATE crawl_spills SET created_at = ?", (now - 7200,))
            conn.commit()
        assert store.gc_stale_spills(3600.0) == 0, "the fence must hold before the release"
        store.release_spill("r1")
        assert store.gc_stale_spills(3600.0) == 1
        assert _row_count(tmp_path) == 0
    finally:
        store.close()


def test_a_live_crawls_rows_survive_a_sweep(tmp_path):
    """A fresh row survives on age alone; the fence covers it too."""
    store = _backdated_store(tmp_path, 1000.0)
    try:
        store.put("r1", 0, engine_result("https://spill.test/0", "rendered"))
        store.gc_stale_spills(3600.0)
        out = store.load_ordered("r1", policy=UNRESTRICTED_HOST_POLICY)
        assert [r.original_url for r in out] == ["https://spill.test/0"]
    finally:
        store.close()


def test_the_async_twin_answers_the_same_ordered_read(tmp_path):
    import asyncio

    async def one():
        store = make_spill_store(tmp_path)
        try:
            await store.aput("r1", 0, engine_result("https://spill.test/0", "rendered"))
            await store.aput("r1", 1, engine_result("https://spill.test/1", "rendered"))
            return await store.aload_ordered("r1", policy=UNRESTRICTED_HOST_POLICY)
        finally:
            store.close()

    out = asyncio.run(one())
    assert [r.original_url for r in out] == ["https://spill.test/0", "https://spill.test/1"]


def test_an_empty_spill_reassembles_to_an_empty_list(tmp_path):
    store = make_spill_store(tmp_path)
    try:
        assert store.load_ordered("r-none", policy=UNRESTRICTED_HOST_POLICY) == []
    finally:
        store.close()


def test_the_row_payload_carries_the_index_and_wire_form(tmp_path):
    """The stored format is the one convention (index, wire result) the old
    spill line carried -- read raw, off the from_wire seam, so the row format
    itself is pinned."""
    store = make_spill_store(tmp_path)
    try:
        store.put("r1", 3, engine_result("https://spill.test/0", "rendered"))
        with sqlite3.connect(tmp_path / "spills.db") as conn:
            blob = conn.execute("SELECT payload FROM crawl_spills").fetchone()[0]
        entry = json.loads(blob)
        assert entry["i"] == 3
        assert entry["r"]["original_url"] == "https://spill.test/0"
    finally:
        store.close()


def test_the_spill_store_schema_drifts_nothing(tmp_path):
    from crawlutron.storage.migrations import CURRENT_SCHEMAS, StoreSchema, schema_drift

    store = make_spill_store(tmp_path)
    try:
        with sqlite3.connect(tmp_path / "spills.db") as conn:
            drift = schema_drift(conn, CURRENT_SCHEMAS[StoreSchema.CRAWL_SPILLS], StoreSchema.CRAWL_SPILLS)
            assert drift == ()
    finally:
        store.close()


def test_the_result_store_is_untouched_by_the_spill_schema(tmp_path):
    """The spill table joins the store family without touching the results
    store's own schema: the sibling store opens on the same database file."""
    db = tmp_path / "shared.db"
    spill_store = CrawlSpillStore(db)
    result_store = CrawlResultStore(db)
    try:
        result_store.put("r1", {"ok": True})
        assert result_store.get("r1") == {"ok": True}
    finally:
        result_store.close()
        spill_store.close()
