"""Control state (status) and data state are separate write planes.

The runner saved the WHOLE row from a pre-engine snapshot, overwriting a
mid-batch pause/cancel; ``save()`` never touches status (atomic CAS only).
"""

import ast
import asyncio
import pathlib
import threading

from crawlutron.models.crawl_policy import CrawlPolicy
from crawlutron.storage.job_store import CrawlJobState, JobStatus, JobStore
from tests._job_harnesses import frontier, loaded_job, make_job_store, run_to_terminal
from tests._policy_factories import canonical
from tests._walk_primitives import REPO_ROOT
from tests.chokepoint_walks import (
    app_trees,
    assert_detector,
    call_nodes,
    enclosing_function,
    nodes_of,
    offender,
    offender_at,
    parent_map,
    resolved_call_name,
)


def _store(tmp_path) -> JobStore:
    return make_job_store(tmp_path)


def _seeded_job(tmp_path, owner: str = "key_a") -> tuple[JobStore, CrawlJobState]:
    """One owned job carrying a visited page; tests add what they need on top."""
    store = _store(tmp_path)
    job = store.create("http://example.com/", CrawlPolicy(), owner=owner)
    loaded = store.load(job.job_id)
    assert loaded is not None
    loaded.visited.add(canonical("http://example.com/page1"))
    store.save(loaded)
    return store, job


def _seeded_job_with_results(tmp_path, owner: str = "key_a") -> tuple[JobStore, CrawlJobState]:
    """One owned job with a visited page and one result row; tests add frontier on top."""
    store, job = _seeded_job(tmp_path, owner=owner)
    loaded = store.load(job.job_id)
    loaded.results = [{"url": "http://example.com/page1", "status": "success"}]
    store.save(loaded)
    return store, job


def assert_history_intact(
    store: JobStore,
    job_id: str,
    caller_id: str,
    *,
    visited: set,
    results: list,
    frontier: list[str],
) -> None:
    """Assert the job row and all accumulator history survive for the caller."""
    after = store.status_for_caller(job_id, caller_id)
    assert after is not None
    assert after["visited_size"] == len(visited)
    assert after["results_count"] == len(results)
    assert after["frontier_size"] == len(frontier)
    surviving = store.load_for_caller(job_id, caller_id)
    assert surviving is not None
    assert surviving.visited == visited
    assert surviving.results == results
    assert [e.url.raw for e in surviving.frontier] == frontier


def _running_job(store: JobStore, job_id: str = "j1") -> CrawlJobState:
    job = CrawlJobState(
        job_id=job_id, root_url="https://a.example/", frontier=frontier(["https://a.example/"])
    )
    store.save(job)
    assert store.set_status(job_id, JobStatus.RUNNING, expected=JobStatus.PENDING) is True
    return job


class TestStatusPlane:
    def test_created_row_persists_the_initial_status(self, tmp_path):
        store = _store(tmp_path)
        store.save(CrawlJobState(job_id="new", root_url="https://a.example/"))
        assert loaded_job(store, "new").status is JobStatus.PENDING

    def test_set_status_transitions_only_from_the_expected_state(self, tmp_path):
        store = _store(tmp_path)
        _running_job(store)
        # Wrong expectation: no write, reported honestly.
        assert store.set_status("j1", JobStatus.CANCELLED, expected=JobStatus.PAUSED) is False
        assert loaded_job(store, "j1").status is JobStatus.RUNNING
        # Correct expectation: atomic flip.
        assert store.set_status("j1", JobStatus.PAUSED, expected=JobStatus.RUNNING) is True
        assert loaded_job(store, "j1").status is JobStatus.PAUSED

    def test_set_status_on_missing_row_is_false(self, tmp_path):
        store = _store(tmp_path)
        assert store.set_status("ghost", JobStatus.CANCELLED, expected=JobStatus.RUNNING) is False

    def test_save_never_clobbers_a_control_transition(self, tmp_path):
        """The store boundary, at the row's own dataclass.

        The runner's snapshot (RUNNING) must not resurrect a paused state, so the
        save below persists frontier/visited/results but not the status.
        """
        store = _store(tmp_path)
        snapshot = _running_job(store)
        # Handler pauses while the runner is inside the engine call.
        assert store.set_status("j1", JobStatus.PAUSED, expected=JobStatus.RUNNING) is True
        # Runner resumes: commits the batch's data from the stale snapshot.
        snapshot.visited.add(canonical("https://a.example/"))
        snapshot.frontier = []
        store.save(snapshot)
        row = loaded_job(store, "j1")
        assert row.status is JobStatus.PAUSED, "data save resurrected the runner's stale status"
        assert row.visited == {canonical("https://a.example/")}  # data plane still flows

    async def test_terminal_claim_respects_a_concurrent_cancel(self, tmp_path):
        """A cancelled job is settled, and nothing may claim otherwise.

        The runner claims COMPLETED but the user cancelled first: the CAS refuses
        and CANCELLED stands. It once restated the terminal set from memory.
        """
        store = _store(tmp_path)
        _running_job(store)
        assert store.set_status("j1", JobStatus.CANCELLED, expected=JobStatus.RUNNING) is True
        assert store.set_status("j1", JobStatus.COMPLETED, expected=JobStatus.RUNNING) is False
        assert loaded_job(store, "j1").status is JobStatus.CANCELLED

        settled = await asyncio.wait_for(run_to_terminal(store, "j1"), timeout=5)
        assert settled.status is JobStatus.CANCELLED


class TestDeleteFinality:
    """DELETE is final: a late runner save must not resurrect the row, and restart
    recovery must not resume a deleted job."""

    @staticmethod
    async def _deleted_job(tmp_path):
        """A store whose job was deleted but then late-saved by its runner."""
        store = _store(tmp_path)
        job = store.create("http://example.com/", CrawlPolicy(), owner="key-a")
        assert await store.adelete_for_caller(job.job_id, "key-a") is True
        # A runner's final asave lands after the delete committed (delete cancels
        # the task without awaiting it, so the save can win the race).
        await store.asave(job)
        return store, job

    async def test_a_late_save_cannot_resurrect_a_deleted_job(self, tmp_path):
        """The store's delete is a tombstone: a racing asave stays a no-op."""
        store, job = await self._deleted_job(tmp_path)

        assert store.load(job.job_id) is None, "a late save resurrected a deleted job"
        assert store.exists(job.job_id) is False
        assert store.list_for_caller("key-a") == []
        assert store.list_jobs() == []

    async def test_restart_recovery_does_not_resume_a_deleted_job(self, tmp_path):
        """A deleted job's resurrected PENDING row must not be re-started on boot."""
        from crawlutron.api.job_recovery import recover_orphaned_running_jobs

        store, job = await self._deleted_job(tmp_path)
        rewound = await recover_orphaned_running_jobs(store)
        assert rewound == 0, "recovery resurrected a deleted job row"
        assert store.load(job.job_id) is None, "the deleted job came back from the dead"


def _status_assignment_offenders() -> list[str]:
    """Any ``X.status = ...`` in app/ outside the store itself.

    The dataclass field is for reads; writing it outside ``JobStore``
    reconstructs the old world where a stale in-memory status reached ``save()``.
    """
    found: list[str] = []
    for path, tree in app_trees():
        if path.name == "job_store.py":
            continue
        for node in nodes_of(tree):
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Attribute) and t.attr == "status" for t in node.targets
            ):
                found.append(offender(path, node.lineno, "assigns.status"))
    return found


def test_status_is_written_only_through_the_store_plane():
    assert not _status_assignment_offenders(), (
        "control-plane field mutated outside JobStore (use set_status): "
        + ", ".join(_status_assignment_offenders())
    )


def test_status_assignment_detector_bites():
    bad = ast.parse("job.status = JobStatus.RUNNING\n")
    hits = [
        n.lineno
        for n in nodes_of(bad)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Attribute) and t.attr == "status" for t in n.targets)
    ]
    assert hits == [1]


def _accumulator_delete_outside_tombstone(tree: ast.Module, path: pathlib.Path) -> list[str]:
    """Flag any ``JobAccumulators.delete`` call not enclosed in ``_tombstone_row``.
    Child-history deletion runs only after a successful ownership UPDATE; a
    where-called invariant, not ordering -- behavior is pinned by tests.
    """
    parents = parent_map(tree)
    out: list[str] = []
    for call in call_nodes(tree):
        if resolved_call_name(tree, call) != "self._accumulators.delete":
            continue
        fn = enclosing_function(call, parents)
        if fn is None or fn.name != "_tombstone_row":
            out.append(offender_at(path, call, "JobAccumulators.delete outside _tombstone_row"))
    return out


def test_accumulator_delete_only_in_tombstone_row():
    """JobAccumulators.delete is only referenced inside _tombstone_row.
    A where-called refactoring guard; ordering is pinned behaviorally by tests.
    """
    offenders: list[str] = []
    for path, tree in app_trees():
        offenders.extend(_accumulator_delete_outside_tombstone(tree, path))
    assert not offenders, f"accumulator delete outside _tombstone_row: {offenders}"


def test_accumulator_delete_detector_bites():
    clean = """
class JobStore:
    def _tombstone_row(self, job_id):
        self._accumulators.delete(job_id)
"""
    bad = """
class Other:
    def purge(self, job_id):
        self._accumulators.delete(job_id)
"""
    assert_detector(
        _accumulator_delete_outside_tombstone,
        path=REPO_ROOT / "crawlutron/storage/job_store.py",
        clean=(clean,),
        trips={bad: 1},
    )


def test_set_status_cannot_flip_a_tombstoned_row(tmp_path):
    """A deleted job's status is frozen: a late runner's CAS must not land (#363)."""
    store = _store(tmp_path)
    job = _running_job(store, "j-gone")
    assert store.delete(job.job_id) is True

    assert store.set_status("j-gone", JobStatus.COMPLETED, expected=JobStatus.RUNNING) is False
    # The row is still gone to every reader: nothing changed, nothing resurrected.
    assert store.load("j-gone") is None
    assert store.exists("j-gone") is False


def test_foreign_delete_for_caller_leaves_history_intact(tmp_path):
    """A foreign caller must not destroy another caller's results + visited + frontier.
    The UPDATE decides ownership before any accumulator row is touched (#C1)."""
    store, job = _seeded_job(tmp_path)
    loaded = store.load(job.job_id)
    loaded.visited.add(canonical("http://example.com/page2"))
    loaded.results = [{"url": "http://example.com/page1", "status": "success"}]
    loaded.frontier_removed.extend(entry.url for entry in loaded.frontier)
    loaded.frontier_mark = 0
    loaded.frontier = frontier(["http://example.com/page3"], depth=1)
    store.save(loaded)

    before = store.status_for_caller(job.job_id, "key_a")
    assert before is not None
    assert before["visited_size"] == 2
    assert before["results_count"] == 1
    assert before["frontier_size"] == 1

    # Foreign caller: nothing matched, nothing deleted; idempotent on the foreign path.
    assert store.delete_for_caller(job.job_id, "key_b") is False
    assert store.delete_for_caller(job.job_id, "key_b") is False

    # Row and all children survive for the owner.
    assert_history_intact(
        store,
        job.job_id,
        "key_a",
        visited={
            canonical("http://example.com/page1"),
            canonical("http://example.com/page2"),
        },
        results=[{"url": "http://example.com/page1", "status": "success"}],
        frontier=["http://example.com/page3"],
    )


def test_delete_for_caller_owner_tombstones_everything(tmp_path):
    """Owner delete removes row, results, visited, and frontier together."""
    store, job = _seeded_job_with_results(tmp_path)

    assert store.delete_for_caller(job.job_id, "key_a") is True
    assert store.load(job.job_id) is None
    assert store.load_for_caller(job.job_id, "key_a") is None
    assert store.status_for_caller(job.job_id, "key_a") is None


def test_delete_ownerless_tombstones_a_live_row(tmp_path):
    """delete() still tombstones a live row (no caller binding) as before."""
    store, job = _seeded_job(tmp_path)

    assert store.delete(job.job_id) is True
    assert store.load(job.job_id) is None
    assert store.exists(job.job_id) is False
    assert store.exists_for_caller(job.job_id, "key_a") is False


def test_concurrent_foreign_deletes_preserve_owner_history(tmp_path):
    """Concurrent foreign delete_for_caller calls are idempotent no-ops: each
    returns False and none destroys owner history (serialized by the lock)."""
    store, job = _seeded_job_with_results(tmp_path)
    loaded = store.load(job.job_id)
    loaded.frontier_removed.extend(entry.url for entry in loaded.frontier)
    loaded.frontier_mark = 0
    loaded.frontier = frontier(["http://example.com/page2"], depth=1)
    store.save(loaded)

    results: list[bool] = []
    barrier = threading.Barrier(8)

    def foreign_delete() -> None:
        barrier.wait()
        results.append(store.delete_for_caller(job.job_id, "key_b"))

    threads = [threading.Thread(target=foreign_delete) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(r is False for r in results), "every foreign delete must return False"
    # History fully intact for the owner.
    assert_history_intact(
        store,
        job.job_id,
        "key_a",
        visited={canonical("http://example.com/page1")},
        results=[{"url": "http://example.com/page1", "status": "success"}],
        frontier=["http://example.com/page2"],
    )

    # Owner delete still works after the foreign storm.
    assert store.delete_for_caller(job.job_id, "key_a") is True
    assert store.load(job.job_id) is None


def test_owner_save_interleaved_with_foreign_deletes(tmp_path):
    """Owner ``save()`` interleaved with foreign deletes: foreign deletes return
    False and the owner's published state lands intact (history not destroyed)."""
    store, job = _seeded_job_with_results(tmp_path)
    loaded = store.load(job.job_id)
    loaded.visited.add(canonical("http://example.com/page2"))
    store.save(loaded)

    results: list[bool] = []

    def foreign_delete() -> None:
        results.append(store.delete_for_caller(job.job_id, "key_b"))

    # Interleave foreign delete with an idempotent owner re-save: every foreign
    # delete must be a no-op and the owner's state unchanged.
    for _ in range(4):
        foreign_delete()
        loaded2 = store.load(job.job_id)
        store.save(loaded2)

    assert all(r is False for r in results), "every foreign delete must return False"
    # Owner state unchanged by the foreign-delete storm.
    assert store.load(job.job_id) is not None
    view = store.status_for_caller(job.job_id, "key_a")
    assert view is not None
    assert view["visited_size"] == 2
    assert view["results_count"] == 1
    assert view["frontier_size"] == 1


class TestErrorStampingGatedOnTheFailedCas:
    """The error field is stamped only when the FAILED CAS lands (#365)."""

    async def test_a_lost_cas_leaves_the_error_field_untouched(self, tmp_path):
        from crawlutron.api.job_runner import arecord_terminal_failure
        from crawlutron.storage.job_store import Expected

        store = make_job_store(tmp_path)
        _running_job(store)
        # The concurrent transition lands between this path's load and its
        # CAS: the load sees RUNNING, but the FAILED write loses the race.

        async def racing_aload(job_id: str):
            return store.load(job_id)

        async def failing_aset_status(
            job_id: str, new: JobStatus, *, expected: Expected, error: str | None = None
        ) -> bool:
            return False

        store.aload = racing_aload
        store.aset_status = failing_aset_status

        landed = await arecord_terminal_failure(store, "j1", "engine exploded")

        assert landed is False
        assert store.load("j1").error is None, "the error was stamped on a row the FAILED CAS did not land on"

    async def test_a_won_cas_stamps_the_error(self, tmp_path):
        from crawlutron.api.job_runner import arecord_terminal_failure

        store = make_job_store(tmp_path)
        _running_job(store)

        landed = await arecord_terminal_failure(store, "j1", "engine exploded")

        assert landed is True
        assert store.load("j1").error == "engine exploded"
        assert store.load("j1").status is JobStatus.FAILED
