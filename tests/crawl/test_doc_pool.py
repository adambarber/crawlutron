"""The persistent markitdown worker pool, asserted through its own constructor.

Every test here builds a :class:`DocExtractionPool` over stub workers instead of
writing the module's slot list: seeding a pool by growing it through its own
acquire is what production does, so an arrangement a test can build is an
arrangement production can reach. The tests that need real child processes keep
the production pool and swap only its ``spawn`` collaborator.
"""

import os
import threading
import time

import pytest

import app.extraction.content as ext_mod
import app.extraction.documents as docex
import app.orchestration.doc_pool as doc_pool_mod
from app.extraction.content import ExtractedContent
from app.orchestration.doc_pool import DocExtractionPool, DocWorker, terminate_doc_process
from tests._app_factory import service_app
from tests._extraction_helpers import settings_with
from tests._lifespan_doubles import stub_heavy_teardown
from tests._lock_doubles import DelegatingLock
from tests._process_doubles import (
    StubConn,
    StubProc,
    StubSpawner,
    install_unkillable_doc_worker,
    stub_proc,
    stub_worker,
)


def _stub_pool(*, spawn, worker_count: int = 1, lock=None) -> DocExtractionPool:
    """A pool of stub workers: one worker wide unless told otherwise."""
    return DocExtractionPool(
        spawn=spawn,
        worker_count=lambda: worker_count,
        enabled=lambda: True,
        lock=lock,
    )


def _seeded(monkeypatch, *workers: DocWorker, lock=None) -> tuple[DocExtractionPool, StubSpawner]:
    """A pool already holding ``workers``, arrived at by a real acquisition.

    The suite used to write the pool's slot list directly, which is the one thing
    a pool owns. Growing to hold known workers through its own ``acquire`` is
    what production does, so a test that seeds this way cannot assert against an
    arrangement production would never produce. Each worker is acquired still
    busy and then released, so the next acquisition grows the pool rather than
    handing back the first one; the spawn record is then cleared, because the
    seeding is how every test got here rather than what any of them are about.
    """
    spawner = StubSpawner(*workers)
    pool = _stub_pool(spawn=spawner, worker_count=len(workers), lock=lock)
    held = [pool.acquire(0.1) for _ in workers]
    assert held == list(workers), "the pool grew slots in an order no test asked for"
    for worker in held:
        pool.release(worker)
    spawner.asked.clear()
    monkeypatch.setattr(docex, "DOC_POOL", pool)
    return pool, spawner


@pytest.fixture
def fake_pool(monkeypatch):
    """Stub the production pool's collaborators and restore it afterwards."""
    monkeypatch.setenv("CRAWLUTRON_EXTRACTION_DOC_WORKERS", "1")
    monkeypatch.setenv("CRAWLUTRON_TEST_HOOKS", "1")
    saved_pool = docex.DOC_POOL
    try:
        yield monkeypatch
    finally:
        docex.DOC_POOL = saved_pool


def _acquire_the_replacement(pool: DocExtractionPool, spawner: StubSpawner, pid: int, bound: float = 0.3):
    """Acquire from a pool whose only slots are unusable; report the spawn too.

    Three tests here ask the same question -- can acquire still grow past a slot
    it refuses to serve? -- and differed only in which bad slot they staged and
    what they then said about the pool afterwards. The short acquire bound is
    part of that arrangement rather than a detail: a pool that cannot grow must
    come back empty instead of hanging, so a test that forgot the bound would
    pass by timing out rather than by the assertion.

    Returns ``(spawned, worker)``: the spawn record -- empty when the pool never
    asked for a replacement -- and the worker acquire handed back.
    """
    spawner.hand_out(stub_worker(pid))
    worker = pool.acquire(bound)
    return spawner.asked, worker


def _acquire_with_bound(pool: DocExtractionPool, bound: float = 0.25):
    """Call ``pool.acquire`` under a small bound, in a thread.

    Returns ``(returned, still_blocked, elapsed)``. The bound is a real argument
    rather than a patched constant, so the deadline this scenario proves is the
    one it asks for; the thread is not a convenience: without a deadline the call
    never returns and the test would hang instead of reporting it.
    """
    box: dict[str, object] = {}

    def _call() -> None:
        box["worker"] = pool.acquire(bound)

    thread = threading.Thread(target=_call, daemon=True)
    started = time.monotonic()
    thread.start()
    thread.join(timeout=bound * 20 + 5)
    return box.get("worker"), thread.is_alive(), time.monotonic() - started


# --- 1: SIGKILL escalation ---------------------------------------------------


def test_terminate_doc_process_escalates_to_sigkill_for_a_stubborn_worker():
    """A worker that absorbs SIGTERM must still be killed, then reaped."""
    proc = stub_proc(responds_to_sigterm=False)
    terminate_doc_process(proc)

    assert proc.signals[:1] == ["SIGTERM"], "graceful shutdown must be attempted first"
    assert "SIGKILL" in proc.signals, f"no escalation after SIGTERM was ignored: {proc.signals}"
    assert not proc.is_alive(), "runaway document worker survived termination"
    assert proc.joined_after_kill, "the killed worker was never reaped (zombie)"


def test_terminate_doc_process_does_not_kill_a_worker_that_exits_cleanly():
    """Inverse case: escalation is for survivors, not the normal path."""
    proc = stub_proc(responds_to_sigterm=True)
    terminate_doc_process(proc)

    assert proc.signals == ["SIGTERM"]
    assert not proc.is_alive()
    assert proc.joined_after_kill is False


# --- 2: a retired worker must never be handed back out ----------------------


def test_retired_stubborn_worker_is_not_acquired_again(fake_pool):
    """The poisoned-slot regression.

    Retirement terminates the worker; if the child survives SIGTERM the slot
    still reports ``is_alive()``, and the old acquire loop read that as "live
    and idle" and served it to the next caller -- which then blocked on a pipe
    nobody was answering, burning a full poll plus join every time.
    """
    zombie = stub_worker(4242, responds_to_sigterm=False)
    pool, spawner = _seeded(fake_pool, zombie)

    pool.retire(zombie)
    spawner.hand_out(stub_worker(9999))

    worker = pool.acquire(settings_with().extraction_doc_acquire_timeout)

    assert spawner.asked, "the retired slot was not replaced"
    assert worker is spawner.asked[-1], f"acquire handed back a retired worker: {worker!r}"
    assert worker.busy is True, "a handed-out worker must be marked busy"


def test_healthy_idle_worker_is_reused_not_respawned(fake_pool):
    """Inverse case: the fix must not turn normal reuse into respawn churn."""
    live = stub_worker(1234)
    pool, spawner = _seeded(fake_pool, live)
    spawner.hand_out(stub_worker(4321))

    worker = pool.acquire(settings_with().extraction_doc_acquire_timeout)

    assert worker is live
    assert worker.busy is True, "a handed-out worker must be marked busy"
    assert spawner.asked == [], "a live idle worker must be reused, not replaced"


# --- 3: bounded acquisition --------------------------------------------------


def test_acquire_gives_up_instead_of_wedging_on_a_stuck_busy_flag(fake_pool):
    """A busy flag nobody clears must cost one document, not the service.

    The pool is at its configured size and the only slot is marked busy, so
    there is nothing to hand out and nothing will ever notify. Acquisition must
    return "no worker" instead of blocking forever.
    """
    stuck = stub_worker(7777)
    pool, _spawner = _seeded(fake_pool, stuck)
    stuck.busy = True  # the holder never comes back for it

    worker, still_blocked, elapsed = _acquire_with_bound(pool)

    assert not still_blocked, "acquire waited forever on a stuck busy flag"
    assert worker is None, f"expected degradation to 'no worker', got {worker!r}"
    assert elapsed < 30.0, f"acquisition should degrade promptly, took {elapsed:.1f}s"


def test_acquire_returns_promptly_when_a_worker_is_released(fake_pool):
    """The bound must not degrade the healthy path: a release wakes a waiter.

    The pool's condition ``wait(timeout)`` has to keep behaving like a condition
    wait -- notified, not polled -- or every document pays the poll interval in
    latency. The ordering is a handshake, not a pause: the waiter announces that
    it has entered acquisition, the main thread only then frees the slot, so a
    condition wait is the only thing that can produce the observed result. The
    old version slept 200ms first and merely observed that nothing had come
    back yet, which the release-then-wait version would also have satisfied.
    """
    busy = stub_worker(5555)
    pool, _spawner = _seeded(fake_pool, busy)
    busy.busy = True

    box: dict[str, object] = {}
    waiter_entered = threading.Event()

    def _call() -> None:
        waiter_entered.set()
        box["worker"] = pool.acquire(settings_with().extraction_doc_acquire_timeout)

    thread = threading.Thread(target=_call, daemon=True)
    thread.start()
    try:
        assert waiter_entered.wait(timeout=5), "the waiter never started acquiring"
        # Hand the slot over and notify, exactly like a real release would.
        pool.release(busy)
        thread.join(timeout=5)
    finally:
        pool.teardown()
    assert not thread.is_alive(), "waiter did not wake when the worker was released"
    assert box.get("worker") is busy


def test_acquire_timeout_is_configured_and_bounded():
    """Same invariant style as the browser pool: the bound exists and is finite."""
    configured = settings_with().extraction_doc_acquire_timeout
    assert configured > 0
    assert configured < 3600


def test_extract_document_degrades_when_no_worker_can_be_acquired(fake_pool, monkeypatch):
    """Caller-visible half: no worker means empty content, not a crash or a hang.

    ``extract_document`` used to assume acquire always succeeded; with a
    bounded acquire it must handle ``None`` the way it handles every other
    document failure -- empty extraction, logged.
    """
    stuck = stub_worker(8888)
    pool, _spawner = _seeded(fake_pool, stuck)
    stuck.busy = True
    settings = settings_with(extraction_doc_acquire_timeout=0.25)

    # The extraction result's own type, so the two reads below are checks about a document-shaped value rather
    # than about `object` having attributes.
    result_box: dict[str, ExtractedContent] = {}

    def _call() -> None:
        result_box["result"] = ext_mod.extract_document(
            b"%PDF-1.4 fake", "http://example.com/x.pdf", settings=settings
        )

    thread = threading.Thread(target=_call, daemon=True)
    thread.start()
    thread.join(timeout=20)

    assert not thread.is_alive(), "extract_document wedged waiting for a worker"
    result = result_box["result"]
    assert result.text is None and result.markdown is None


# --- 4/5/6: -----------------------------------------------------------------


class _LockWatcherProc(StubProc):
    """A dead worker whose termination reports whether the pool lock was held.

    The kill-escalation in ``terminate_doc_process`` can take up to ~10s
    (SIGTERM -> join(5) -> SIGKILL -> join(5)). Running it under the pool lock
    blocks every other acquisition and every release for that whole window, so
    the termination must happen off-lock. ``terminate`` is the first slow step
    of that escalation, so it is where the lock state is observed.
    """

    def __init__(self, lock, *, alive: bool = False) -> None:
        super().__init__()
        self._alive = alive
        self._lock = lock
        self.lock_held_during_terminate: list[bool] = []

    def terminate(self) -> None:
        free = self._lock.acquire(blocking=False)
        try:
            self.lock_held_during_terminate.append(not free)
        finally:
            if free:
                self._lock.release()
        super().terminate()


def test_acquire_budget_starts_after_the_lock_not_before_it(fake_pool):
    """Time spent queued on the pool lock is not acquire budget.
    A caller blocked behind a slow in-lock operation must not find its whole
    budget already spent when it finally enters -- it would then report "no
    worker" for a pool that had a usable slot freed the entire time it waited.
    The deadline is therefore computed after the lock is acquired.

    The holder waits a real, bounded span under the lock -- the adverse
    condition, and the only reason a wall clock appears here at all. The
    assertion is on what the acquirer returned, and the test proves its own
    precondition first: the acquirer is observed INSIDE the lock, with the
    slot still busy, at the moment the budget is computed. The mutant
    (budget computed before the lock) then leaves it with an elapsed budget,
    it gives up, and ``box["worker"]`` is ``None`` -- deterministically, not
    by a race over which thread wins the lock.
    """
    bound = 0.5
    worker = stub_worker(6060)
    pool_lock = threading.Condition(threading.Lock())

    # The pool lock is instrumented so the test can PROVE the precondition it is asserting about: the acquirer
    # has to be inside the lock, with the slot still busy, at the moment its budget is computed.
    acquirer_thread: dict[str, int] = {}
    acquirer_inside = threading.Event()

    class _RecordingLock(DelegatingLock):
        def acquire(self, *args, **kwargs):
            got = super().acquire(*args, **kwargs)
            if threading.get_ident() == acquirer_thread.get("id"):
                acquirer_inside.set()
            return got

    recording_lock = _RecordingLock(pool_lock)
    pool, _spawner = _seeded(fake_pool, worker, lock=recording_lock)
    worker.busy = True

    box: dict[str, object] = {}

    def _acquire() -> None:
        acquirer_thread["id"] = threading.get_ident()
        box["worker"] = pool.acquire(bound)

    holder_ready = threading.Event()
    holder_done = threading.Event()

    def _hold_then_free() -> None:
        # Hold the pool lock past the acquirer's budget: this is the blocked-behind-a-slow-retirement window.
        # The slot is deliberately STILL busy at release, so an acquirer that enters must wait.
        with pool_lock:
            holder_ready.set()
            threading.Event().wait(bound * 2)
        # The acquirer is now inside the lock with the slot still busy and its budget already elapsed --
        # exactly the failure point. Only now is the slot freed and the waiter notified, like a real release.
        assert acquirer_inside.wait(timeout=10), "the acquirer never entered the pool lock"
        pool.release(worker)
        holder_done.set()

    acquirer = threading.Thread(target=_acquire, daemon=True)
    holder = threading.Thread(target=_hold_then_free, daemon=True)
    holder.start()
    assert holder_ready.wait(timeout=5), "the lock holder never started"
    acquirer.start()
    try:
        acquirer.join(timeout=10)
    finally:
        holder_done.wait(timeout=10)
        holder.join(timeout=10)

    assert not acquirer.is_alive(), "acquire wedged behind the lock holder"
    assert box.get("worker") is worker, (
        "acquire burned its whole budget waiting for the pool lock and then gave "
        "up, even though a usable slot was freed while it waited"
    )


def test_respawn_terminates_the_replaced_worker_off_the_pool_lock(fake_pool):
    """The replaced worker's kill-escalation must not hold the pool lock.

    The retirement is what can take ~10s; the slot swap itself is instant. The
    pool lock only needs to cover the swap, so the termination is deferred until
    after it. A probe in ``terminate`` proves the lock was free at that moment.
    """
    pool_lock = threading.Condition(threading.Lock())
    dead = DocWorker(_LockWatcherProc(pool_lock, alive=False), StubConn())
    dead.busy = False
    pool, spawner = _seeded(fake_pool, dead, lock=pool_lock)
    spawner.hand_out(stub_worker(9090))

    worker = pool.acquire(settings_with().extraction_doc_acquire_timeout)

    assert worker is spawner.asked[-1], "the dead slot was not respawned"
    assert worker.busy is True
    assert dead.retired is True, "the replaced worker was never marked retired"
    assert dead.proc.lock_held_during_terminate == [False], (
        "the replaced worker was terminated while holding the pool lock; the "
        "kill-escalation would block every other acquire and release"
    )


def test_undead_retired_slot_does_not_consume_a_pool_slot(fake_pool):
    """A slot that will not die must not permanently shrink the pool.

    A retired-but-still-``is_alive()`` slot is skipped by BOTH scans (handout
    requires ``not retired``; respawn requires ``not is_alive``), so nothing in
    the normal path can ever recover it. If it still counts toward the size
    budget, the pool is one worker short forever and every later acquire
    degrades to "no worker" instead of growing past the zombie slot.
    """
    _zombie, pool, spawner = install_unkillable_doc_worker()

    spawned, worker = _acquire_the_replacement(pool, spawner, 31337)

    assert spawned, "the pool did not grow past the unkillable slot"
    assert worker is spawned[0], (
        "acquire gave up instead of growing: an unkillable slot permanently consumed a pool-size budget"
    )


async def _noop() -> None:
    return None


@pytest.fixture
def seeded_doc_pool(monkeypatch):
    """A live document worker in the pool, restored afterwards."""
    worker = stub_worker(4242)
    pool, _spawner = _seeded(monkeypatch, worker)
    yield worker, pool


class TestLifespanDocPoolTeardown:
    async def test_shutdown_retires_the_document_workers(self, monkeypatch, seeded_doc_pool):
        """The document pool must be torn down by the production lifespan.

        Asserted by effect, not by spying: a live worker is seeded into the pool
        before the app runs, and shutdown must retire it and drop the pool. The
        heavy client teardowns are stubbed only so the test does not start real
        services; the document teardown itself runs for real.
        """
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


def _counted_spawns(monkeypatch):
    """Count pool worker constructions parent-side (spawn-safe: no pickling).

    The production pool keeps its real workers and the real task hooks; only the
    ``spawn`` collaborator is wrapped, which is the seam the pool was given so
    "how many children did this cost" is answerable without counting children.
    """
    constructions = 0
    real_spawn = doc_pool_mod.spawn_doc_worker

    def counting_spawn() -> DocWorker:
        nonlocal constructions
        constructions += 1
        return real_spawn()

    monkeypatch.setattr(
        docex,
        "DOC_POOL",
        DocExtractionPool(
            spawn=counting_spawn,
            worker_count=docex.doc_worker_count,
            enabled=lambda: True,
        ),
    )
    return lambda: constructions


def _use_single_worker(monkeypatch):
    monkeypatch.setenv("CRAWLUTRON_EXTRACTION_DOC_WORKERS", "1")
    monkeypatch.setenv("CRAWLUTRON_TEST_HOOKS", "1")
    monkeypatch.setenv("TEST_MARKITDOWN_OUTPUT", "## Hello Markdown")
    docex.DOC_POOL.teardown()


def _restore_pool():
    for var in ("TEST_MARKITDOWN_OUTPUT", "TEST_MARKITDOWN_HANG", "TEST_MARKITDOWN_DIE"):
        os.environ.pop(var, None)
    os.environ.pop("CRAWLUTRON_TEST_HOOKS", None)
    os.environ.pop("CRAWLUTRON_EXTRACTION_DOC_WORKERS", None)
    docex.DOC_POOL.teardown()


def test_doc_workers_persist_across_documents(monkeypatch):
    """N documents cost one worker construction, not N spawns."""
    constructions = _counted_spawns(monkeypatch)
    _use_single_worker(monkeypatch)
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
    constructions = _counted_spawns(monkeypatch)
    _use_single_worker(monkeypatch)
    try:
        monkeypatch.setenv("TEST_MARKITDOWN_HANG", "1")
        started = time.monotonic()
        result = ext_mod.extract_document(
            b"%PDF-1.4 fake",
            "http://example.com/hang.pdf",
            settings=settings_with(extraction_doc_timeout=0.05),
        )
        assert time.monotonic() - started < 5.0
        assert result.text is None and result.markdown is None
        hung_pids = docex.DOC_POOL.pids()

        monkeypatch.delenv("TEST_MARKITDOWN_HANG")
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
    """-D: a retired slot whose process will not die is skipped by both
    the handout scan (needs ``not retired``) and the respawn scan (needs
    ``not is_alive``), so nothing can ever recover it. Leaving it in the pool
    means it occupies a list slot -- and holds a PID -- forever while the size
    budget grows around it. It must be reaped on the normal path, exactly like
    full teardown does, so the pool and the budget stay consistent.
    """
    zombie, pool, spawner = install_unkillable_doc_worker()

    spawned, worker = _acquire_the_replacement(pool, spawner, 31337)

    assert worker is spawned[0], "the pool did not grow past the unkillable slot"
    assert zombie.proc.pid not in pool.pids(), (
        "the retired-but-undead slot was left in the pool: it can never be "
        "recovered and will occupy a slot for the life of the process"
    )


def test_a_retired_dead_slot_is_also_reaped_not_left_as_a_placeholder(fake_pool):
    """The reap is not conditional on the process being undead: a slot that was
    retired after a normal timeout (process died and was reaped) must not
    linger either. The pool and the budget stay consistent only when no retired
    slot remains, so a later acquire sees ``len(pool) == usable``."""
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
