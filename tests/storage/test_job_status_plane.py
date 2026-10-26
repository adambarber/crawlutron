"""Control state (status) and data state are separate write planes.

The runner saved the WHOLE row from a pre-engine snapshot, overwriting a
mid-batch pause/cancel; ``save()`` never touches status (atomic CAS only).
"""

import ast
import asyncio

from app.models.crawl_policy import CrawlPolicy
from app.storage.job_store import CrawlJobState, JobStatus, JobStore
from tests._job_harnesses import frontier, loaded_job, make_job_store, run_to_terminal
from tests._policy_factories import canonical
from tests.chokepoint_walks import app_trees, nodes_of, offender


def _store(tmp_path) -> JobStore:
    return make_job_store(tmp_path)


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
        from app.api.job_recovery import recover_orphaned_running_jobs

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


def test_set_status_cannot_flip_a_tombstoned_row(tmp_path):
    """A deleted job's status is frozen: a late runner's CAS must not land (#363)."""
    store = _store(tmp_path)
    job = _running_job(store, "j-gone")
    assert store.delete(job.job_id) is True

    assert store.set_status("j-gone", JobStatus.COMPLETED, expected=JobStatus.RUNNING) is False
    # The row is still gone to every reader: nothing changed, nothing resurrected.
    assert store.load("j-gone") is None
    assert store.exists("j-gone") is False
