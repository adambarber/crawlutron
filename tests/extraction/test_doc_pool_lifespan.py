import os
import time

import app.extraction.content as ext_mod
import app.extraction.doc_pool as doc_pool_mod
import app.extraction.documents as docex
from app.extraction.doc_pool import DocWorker, install_doc_pool_hooks
from tests._app_factory import service_app
from tests._doc_pool_fixtures import _seeded, fresh_doc_pool
from tests._extraction_harnesses import acquire_the_replacement as _acquire_the_replacement
from tests._extraction_helpers import settings_with
from tests._lifespan_doubles import stub_heavy_teardown
from tests._process_doubles import install_unkillable_doc_worker, stub_worker


class TestLifespanDocPoolTeardown:
    async def test_shutdown_retires_the_document_workers(self, monkeypatch, seeded_doc_pool):
        """The document pool must be torn down by the production lifespan.

        Asserted by effect: a live worker is seeded before the app runs, and
        shutdown must retire it and drop the pool."""
        stub_heavy_teardown(monkeypatch)
        worker, pool = seeded_doc_pool
        app = service_app()

        async with app.router.lifespan_context(app):
            # Startup does not touch the document pool, and the worker must still be live inside the app: the
            # teardown is a shutdown concern.
            assert worker.proc.terminated is False, "startup retired a document worker"
            assert pool.pids() == [worker.proc.pid], "the seeded worker is not in the pool"

        assert worker.proc.terminated, (
            "lifespan shutdown did not retire the document worker; the pool was "
            "torn down only by the test fixture"
        )
        assert worker.conn.closed, "shutdown left the worker's pipe open"
        assert pool.pids() == [], "the document pool survived shutdown"


def _counting_spawn(monkeypatch):
    """A spawn wrapper counting constructions, and a read back for the count.

    Only the ``spawn`` collaborator is wrapped -- the seam that makes "how
    many children did this cost" answerable without counting children."""
    constructions = 0
    real_spawn = doc_pool_mod.spawn_doc_worker

    def counting_spawn(ready_timeout: float) -> DocWorker:
        nonlocal constructions
        constructions += 1
        return real_spawn()

    return counting_spawn, lambda: constructions


def _use_single_worker(monkeypatch, *, spawn=None):
    monkeypatch.setenv("CRAWLUTRON_EXTRACTION_DOC_WORKERS", "1")
    install_doc_pool_hooks({"output": "## Hello Markdown"})
    # teardown() is terminal since #246 -- it can no longer double as a reset, so
    # an isolated test gets a fresh pool instead of closing the shared one.
    fresh_doc_pool(monkeypatch, spawn=spawn)


def _restore_pool():
    install_doc_pool_hooks(None)
    os.environ.pop("CRAWLUTRON_EXTRACTION_DOC_WORKERS", None)


def test_doc_workers_persist_across_documents(monkeypatch):
    """N documents cost one worker construction, not N spawns."""
    counting_spawn, constructions = _counting_spawn(monkeypatch)
    _use_single_worker(monkeypatch, spawn=counting_spawn)
    try:
        for _ in range(3):
            result = ext_mod.extract_document(b"%PDF-1.4 fake", "http://example.com/x.pdf")
            assert result.markdown == "## Hello Markdown"
        assert constructions() == 1
        assert len(docex.DOC_POOL.pids()) == 1
    finally:
        _restore_pool()


def test_doc_worker_pids_stable_across_tasks(monkeypatch):
    """The same worker serves consecutive documents (no respawn churn)."""
    _use_single_worker(monkeypatch)
    try:
        ext_mod.extract_document(b"%PDF-1.4 fake", "http://example.com/a.pdf")
        first = docex.DOC_POOL.pids()
        ext_mod.extract_document(b"%PDF-1.4 fake", "http://example.com/b.pdf")
        second = docex.DOC_POOL.pids()
        assert first == second
        assert all(pid > 0 for pid in first)
    finally:
        _restore_pool()


def test_doc_timeout_kills_and_pool_recovers(monkeypatch):
    """A hung worker is killed on timeout; the pool serves the next document."""
    counting_spawn, constructions = _counting_spawn(monkeypatch)
    _use_single_worker(monkeypatch, spawn=counting_spawn)
    try:
        install_doc_pool_hooks({"hang": True})
        started = time.monotonic()
        result = ext_mod.extract_document(
            b"%PDF-1.4 fake",
            "http://example.com/hang.pdf",
            settings=settings_with(extraction_doc_timeout=0.05),
        )
        assert time.monotonic() - started < 5.0
        assert result.text is None and result.markdown is None
        hung_pids = docex.DOC_POOL.pids()

        install_doc_pool_hooks({"output": "## Hello Markdown"})
        # A normal deadline for recovery: the respawned worker must boot (spawn platforms) before the task
        # timeout starts mattering again.
        recovered = ext_mod.extract_document(
            b"%PDF-1.4 fake",
            "http://example.com/ok.pdf",
            settings=settings_with(extraction_doc_timeout=10.0),
        )
        assert recovered.markdown == "## Hello Markdown"
        # The hung worker was killed and its slot respawned: exactly one replacement construction, pool back
        # at one live worker.
        assert constructions() == 2
        live_pids = docex.DOC_POOL.pids()
        assert len(live_pids) == 1
        assert live_pids != hung_pids
    finally:
        _restore_pool()


def test_undead_retired_slot_is_reaped_from_the_pool(fake_pool):
    """A retired slot whose process will not die must be reaped on the normal
    path: it is skipped by both scans, so it occupies a slot -- and holds a
    PID -- forever while the size budget grows around it."""
    zombie, pool, spawner = install_unkillable_doc_worker()

    spawned, worker = _acquire_the_replacement(pool, spawner, 31337)

    assert worker is spawned[0], "the pool did not grow past the unkillable slot"
    assert zombie.proc.pid not in pool.pids(), (
        "the retired-but-undead slot was left in the pool: it can never be "
        "recovered and will occupy a slot for the life of the process"
    )


def test_a_retired_dead_slot_is_also_reaped_not_left_as_a_placeholder(fake_pool):
    """The reap is not conditional on the process being undead: a slot retired
    after a normal timeout must not linger either -- no retired slot remains."""
    retired = stub_worker(2323)
    retired.retired = True
    retired.busy = False
    pool, spawner = _seeded(fake_pool, retired)

    spawned, worker = _acquire_the_replacement(pool, spawner, 4242)

    assert worker is spawned[0]
    assert retired.proc.pid not in pool.pids(), (
        "a retired slot was left in the pool as a placeholder instead of reaped"
    )
    assert len(pool) == len(pool.pids()), (
        "the pool holds a retired slot: the pool size and the usable budget have drifted apart"
    )
