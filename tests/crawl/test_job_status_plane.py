"""Control state (status) and data state are separate write planes.

The runner saved the WHOLE row from a pre-engine snapshot, overwriting a
mid-batch pause/cancel; ``save()`` never touches status (atomic CAS only).
"""

import ast
import asyncio

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
        assert row.visited == {canonical("https://a.example")}  # data plane still flows

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
