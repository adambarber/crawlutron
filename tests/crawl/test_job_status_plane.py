"""Control state (status) and data state are separate write planes.

The runner reloads its row every loop iteration and saves the WHOLE row --
including ``status`` -- from a pre-engine snapshot. A pause/cancel handled
mid-batch was silently overwritten by the runner's next save, and the
post-save check read the same stale in-memory copy, so the transition could
never be observed. Fix: ``save()`` never touches the status column; every
transition goes through ``set_status(job_id, new, expected=...)``, an
atomic compare-and-set on the row itself.
"""

import ast

from app.crawl.job_store import CrawlJobState, JobStatus, JobStore
from tests._job_harnesses import frontier, loaded_job, make_job_store
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
        """The, at the store boundary.

        The runner holds a pre-engine snapshot (status RUNNING); a handler
        pauses the row mid-batch; the runner's next data save must persist
        frontier/visited/results but MUST NOT resurrect RUNNING.
        """
        store = _store(tmp_path)
        snapshot = _running_job(store)
        # Handler pauses while the runner is inside the engine call.
        assert store.set_status("j1", JobStatus.PAUSED, expected=JobStatus.RUNNING) is True
        # Runner resumes: commits the batch's data from the stale snapshot.
        snapshot.visited.add("https://a.example/")
        snapshot.frontier = []
        store.save(snapshot)
        row = loaded_job(store, "j1")
        assert row.status is JobStatus.PAUSED, "data save resurrected the runner's stale status"
        assert row.visited == {"https://a.example"}  # data plane still flows

    def test_terminal_claim_respects_a_concurrent_cancel(self, tmp_path):
        """Runner finishes the last batch and claims COMPLETED, but the user
        cancelled first: the CAS refuses and CANCELLED stands."""
        store = _store(tmp_path)
        _running_job(store)
        assert store.set_status("j1", JobStatus.CANCELLED, expected=JobStatus.RUNNING) is True
        assert store.set_status("j1", JobStatus.COMPLETED, expected=JobStatus.RUNNING) is False
        assert loaded_job(store, "j1").status is JobStatus.CANCELLED


def _status_assignment_offenders() -> list[str]:
    """Any ``X.status = ...`` in app/ outside the store itself.

    The dataclass field exists for reads (loaded rows carry it); writing it
    outside ``JobStore`` reconstructs the old world where an in-memory
    status view could reach ``save()`` and clobber a control transition.
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
