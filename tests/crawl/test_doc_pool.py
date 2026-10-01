"""Document extraction pool: worker lifecycle, kill escalation, teardown and process-pool reuse."""

import threading
import time

import pytest

import app.crawl.extraction as ext_mod
from app.crawl.extraction import ExtractedContent
from tests._app_factory import service_app
from tests._extraction_helpers import settings_with
from tests._lifespan_doubles import stub_heavy_teardown
from tests._lock_doubles import DelegatingLock
from tests._process_doubles import (
    StubConn,
    StubProc,
    install_unkillable_doc_worker,
    stub_proc,
)


def _stub_worker(pid: int, responds_to_sigterm: bool = True) -> ext_mod._DocWorker:
    worker = ext_mod._DocWorker(StubProc(pid, responds_to_sigterm), StubConn())
    worker.busy = False
    return worker


@pytest.fixture
def fake_pool(monkeypatch):
    """Give the pool stub slots, one worker wide, and restore it afterwards."""
    monkeypatch.setenv("CRAWLUTRON_EXTRACTION_DOC_WORKERS", "1")
    monkeypatch.setenv("CRAWLUTRON_TEST_HOOKS", "1")
    saved_pool = ext_mod._DOC_POOL
    saved_spawn = ext_mod._spawn_doc_worker
    with ext_mod._POOL_LOCK:
        ext_mod._DOC_POOL = []
    try:
        yield monkeypatch
    finally:
        with ext_mod._POOL_LOCK:
            ext_mod._DOC_POOL = saved_pool
        ext_mod._spawn_doc_worker = saved_spawn


def _spawn_replacement(monkeypatch, pid: int) -> list[ext_mod._DocWorker]:
    """Make the pool's spawn hand out one stub worker; return the spawn record.

    Every "the pool replaced a slot" test needs the same pair: a spawn that
    returns a known worker, and proof that a spawn happened at all. Asserting
    only that the handed-out worker is the replacement would pass on a pool that
    grew the slot some other way, so the record is returned and asserted too --
    it is empty until the pool actually asked.
    """
    replacement = _stub_worker(pid)
    spawned: list[ext_mod._DocWorker] = []

    def _fake_spawn():
        spawned.append(replacement)
        return replacement

    monkeypatch.setattr(ext_mod, "_spawn_doc_worker", _fake_spawn)
    return spawned


def _acquire_the_replacement(fake_pool, pid: int, bound: float = 0.3):
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
    spawned = _spawn_replacement(fake_pool, pid)
    return spawned, ext_mod._acquire_doc_worker(bound)


def _acquire_with_bound(bound: float = 0.25):
    """Call ``_acquire_doc_worker`` under a small acquire bound, in a thread.

    Returns ``(returned, still_blocked, elapsed)``. The bound is a real argument
    rather than a patched module constant, so the deadline this scenario proves
    is the one it asks for; the thread is not a convenience: without a deadline
    the call never returns and the test would hang instead of reporting it.
    """
    box: dict[str, object] = {}

    def _call() -> None:
        box["worker"] = ext_mod._acquire_doc_worker(bound)

    thread = threading.Thread(target=_call, daemon=True)
    started = time.monotonic()
    thread.start()
    thread.join(timeout=bound * 20 + 5)
    return box.get("worker"), thread.is_alive(), time.monotonic() - started


# --- 1: SIGKILL escalation ---------------------------------------------------


def test_terminate_doc_process_escalates_to_sigkill_for_a_stubborn_worker():
    """A worker that absorbs SIGTERM must still be killed, then reaped."""
    proc = stub_proc(responds_to_sigterm=False)
    ext_mod._terminate_doc_process(proc)

    assert proc.signals[:1] == ["SIGTERM"], "graceful shutdown must be attempted first"
    assert "SIGKILL" in proc.signals, f"no escalation after SIGTERM was ignored: {proc.signals}"
    assert not proc.is_alive(), "runaway document worker survived termination"
    assert proc.joined_after_kill, "the killed worker was never reaped (zombie)"


def test_terminate_doc_process_does_not_kill_a_worker_that_exits_cleanly():
    """Inverse case: escalation is for survivors, not the normal path."""
    proc = stub_proc(responds_to_sigterm=True)
    ext_mod._terminate_doc_process(proc)

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
    zombie = _stub_worker(4242, responds_to_sigterm=False)
    with ext_mod._POOL_LOCK:
        ext_mod._DOC_POOL = [zombie]

    ext_mod._retire_doc_worker(zombie)

    spawned = _spawn_replacement(fake_pool, 9999)

    worker = ext_mod._acquire_doc_worker(settings_with().extraction_doc_acquire_timeout)

    assert spawned, "the retired slot was not replaced"
    assert worker is spawned[0], f"acquire handed back a retired worker: {worker!r}"
    assert worker.busy is True, "a handed-out worker must be marked busy"


def test_healthy_idle_worker_is_reused_not_respawned(fake_pool):
    """Inverse case: the fix must not turn normal reuse into respawn churn."""
    live = _stub_worker(1234)
    with ext_mod._POOL_LOCK:
        ext_mod._DOC_POOL = [live]

    def _should_not_spawn():  # pragma: no cover - proves no respawn
        raise AssertionError("a live idle worker must be reused, not replaced")

    fake_pool.setattr(ext_mod, "_spawn_doc_worker", _should_not_spawn)

    assert ext_mod._acquire_doc_worker(settings_with().extraction_doc_acquire_timeout) is live


# --- 3: bounded acquisition --------------------------------------------------


def test_acquire_gives_up_instead_of_wedging_on_a_stuck_busy_flag(fake_pool):
    """A busy flag nobody clears must cost one document, not the service.

    The pool is at its configured size and the only slot is marked busy, so
    there is nothing to hand out and nothing will ever notify. Acquisition must
    return "no worker" instead of blocking forever.
    """
    stuck = _stub_worker(7777)
    stuck.busy = True
    with ext_mod._POOL_LOCK:
        ext_mod._DOC_POOL = [stuck]

    worker, still_blocked, elapsed = _acquire_with_bound()

    assert not still_blocked, "_acquire_doc_worker waited forever on a stuck busy flag"
    assert worker is None, f"expected degradation to 'no worker', got {worker!r}"
    assert elapsed < 30.0, f"acquisition should degrade promptly, took {elapsed:.1f}s"


def test_acquire_returns_promptly_when_a_worker_is_released(fake_pool):
    """The bound must not degrade the healthy path: a release wakes a waiter.

    ``_POOL_LOCK.wait(timeout)`` has to keep behaving like a condition wait --
    notified, not polled -- or every document pays the poll interval in latency.
    The ordering is a handshake, not a pause: the waiter announces that it has
    entered acquisition, the main thread only then frees the slot, so a
    condition wait is the only thing that can produce the observed result. The
    old version slept 200ms first and merely observed that nothing had come
    back yet, which the release-then-wait version would also have satisfied.
    """
    busy = _stub_worker(5555)
    busy.busy = True
    with ext_mod._POOL_LOCK:
        ext_mod._DOC_POOL = [busy]

    box: dict[str, object] = {}
    waiter_entered = threading.Event()

    def _call() -> None:
        waiter_entered.set()
        # The configured bound, not a short one: the claim is that a release
        # wakes the waiter, and that only holds if the waiter was still waiting
        # when the release landed.
        box["worker"] = ext_mod._acquire_doc_worker(settings_with().extraction_doc_acquire_timeout)

    thread = threading.Thread(target=_call, daemon=True)
    thread.start()
    try:
        assert waiter_entered.wait(timeout=5), "the waiter never started acquiring"
        # Hand the slot over and notify, exactly like a real release would.
        with ext_mod._POOL_LOCK:
            busy.busy = False
            ext_mod._POOL_LOCK.notify_all()
        thread.join(timeout=5)
    finally:
        with ext_mod._POOL_LOCK:
            ext_mod._DOC_POOL = []
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
    stuck = _stub_worker(8888)
    stuck.busy = True
    with ext_mod._POOL_LOCK:
        ext_mod._DOC_POOL = [stuck]
    settings = settings_with(extraction_doc_acquire_timeout=0.25)

    # The extraction result's own type, so the two reads below are checks about
    # a document-shaped value rather than about `object` having attributes.
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


# --- 4/5/6: ---------------------------------------


class _LockWatcherProc(StubProc):
    """A dead worker whose termination reports whether ``_POOL_LOCK`` was held.

    The kill-escalation in ``_terminate_doc_process`` can take up to ~10s
    (SIGTERM -> join(5) -> SIGKILL -> join(5)). Running it under the pool lock
    blocks every other acquisition and every release for that whole window, so
    the termination must happen off-lock. ``terminate`` is the first slow step
    of that escalation, so it is where the lock state is observed.
    """

    def __init__(self, *, alive: bool = False) -> None:
        super().__init__()
        self._alive = alive
        self.lock_held_during_terminate: list[bool] = []

    def terminate(self) -> None:
        free = ext_mod._POOL_LOCK.acquire(blocking=False)
        try:
            self.lock_held_during_terminate.append(not free)
        finally:
            if free:
                ext_mod._POOL_LOCK.release()
        super().terminate()


def test_acquire_budget_starts_after_the_lock_not_before_it(fake_pool):
    """Time spent queued on ``_POOL_LOCK`` is not acquire budget.
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
    worker = _stub_worker(6060)
    worker.busy = True
    with ext_mod._POOL_LOCK:
        ext_mod._DOC_POOL = [worker]

    # The pool lock is instrumented so the test can PROVE the precondition
    # it is asserting about: the acquirer has to be inside the lock, with
    # the slot still busy, at the moment its budget is computed. Without
    # that, an acquirer that happened to enter after the holder freed the
    # slot would collect the worker with a budget it never spent and the
    # test would pass against the it exists to catch.
    acquirer_thread: dict[str, int] = {}
    acquirer_inside = threading.Event()

    class _RecordingLock(DelegatingLock):
        def acquire(self, *args, **kwargs):
            got = super().acquire(*args, **kwargs)
            if threading.get_ident() == acquirer_thread.get("id"):
                acquirer_inside.set()
            return got

    real_lock = ext_mod._POOL_LOCK
    fake_pool.setattr(ext_mod, "_POOL_LOCK", _RecordingLock(real_lock), raising=False)

    box: dict[str, object] = {}

    def _acquire() -> None:
        acquirer_thread["id"] = threading.get_ident()
        box["worker"] = ext_mod._acquire_doc_worker(bound)

    holder_ready = threading.Event()
    holder_done = threading.Event()

    def _hold_then_free() -> None:
        # Hold the pool lock past the acquirer's budget: this is the
        # blocked-behind-a-slow-retirement window. The slot is deliberately
        # STILL busy at release, so an acquirer that enters must wait.
        # ``Event.wait`` rather than ``time.sleep``: the suite patches the
        # latter to a no-op, and the lock has to be held for real seconds or
        # the budget is never actually exceeded.
        with ext_mod._POOL_LOCK:
            holder_ready.set()
            threading.Event().wait(bound * 2)
        # The acquirer is now inside the lock with the slot still busy and
        # its budget already elapsed -- exactly the failure point. Only
        # now is the slot freed and the waiter notified, like a real release.
        assert acquirer_inside.wait(timeout=10), "the acquirer never entered the pool lock"
        with ext_mod._POOL_LOCK:
            worker.busy = False
            ext_mod._POOL_LOCK.notify_all()
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

    assert not acquirer.is_alive(), "_acquire_doc_worker wedged behind the lock holder"
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
    dead = ext_mod._DocWorker(_LockWatcherProc(alive=False), StubConn())
    dead.busy = False
    with ext_mod._POOL_LOCK:
        ext_mod._DOC_POOL = [dead]

    replacement = _stub_worker(9090)

    def _fake_spawn():
        return replacement

    fake_pool.setattr(ext_mod, "_spawn_doc_worker", _fake_spawn)

    worker = ext_mod._acquire_doc_worker(settings_with().extraction_doc_acquire_timeout)

    assert worker is replacement, "the dead slot was not respawned"
    assert worker.busy is True
    assert dead.retired is True, "the replaced worker was never marked retired"
    assert dead.proc.lock_held_during_terminate == [False], (
        "the replaced worker was terminated while holding _POOL_LOCK; the "
        "kill-escalation would block every other acquire and release"
    )


def test_undead_retired_slot_does_not_consume_a_pool_slot(fake_pool, monkeypatch):
    """A slot that will not die must not permanently shrink the pool.

    A retired-but-still-``is_alive()`` slot is skipped by BOTH scans (handout
    requires ``not retired``; respawn requires ``not is_alive``), so nothing in
    the normal path can ever recover it. If it still counts toward the size
    budget, the pool is one worker short forever and every later acquire
    degrades to "no worker" instead of growing past the zombie slot.
    """
    install_unkillable_doc_worker()

    spawned, worker = _acquire_the_replacement(fake_pool, 31337)

    assert spawned, "the pool did not grow past the unkillable slot"
    assert worker is spawned[0], (
        "acquire gave up instead of growing: an unkillable slot permanently consumed a pool-size budget"
    )


async def _noop() -> None:
    return None


@pytest.fixture
def seeded_doc_pool(monkeypatch):
    """A live document worker in the pool, restored afterwards."""
    from app.crawl import extraction as ext_mod

    worker = ext_mod._DocWorker(StubProc(), StubConn())
    saved_pool = ext_mod._DOC_POOL
    with ext_mod._POOL_LOCK:
        ext_mod._DOC_POOL = [worker]
    try:
        yield worker
    finally:
        with ext_mod._POOL_LOCK:
            ext_mod._DOC_POOL = saved_pool


class TestLifespanDocPoolTeardown:
    async def test_shutdown_retires_the_document_workers(self, monkeypatch, seeded_doc_pool):
        """The document pool must be torn down by the production lifespan.

        Asserted by effect, not by spying: a live worker is seeded into the pool
        before the app runs, and shutdown must retire it and drop the pool. The
        heavy client teardowns are stubbed only so the test does not start real
        services; the document teardown itself runs for real.
        """
        from app.crawl import extraction as ext_mod

        stub_heavy_teardown(monkeypatch)
        worker = seeded_doc_pool
        app = service_app()

        async with app.router.lifespan_context(app):
            # Startup does not touch the document pool, and the worker must
            # still be live inside the app: the teardown is a shutdown concern.
            assert worker.proc.terminated is False, "startup retired a document worker"
            with ext_mod._POOL_LOCK:
                assert ext_mod._DOC_POOL == [worker]

        assert worker.proc.terminated, (
            "lifespan shutdown did not retire the document worker; the pool was "
            "torn down only by the test fixture"
        )
        assert worker.conn.closed, "shutdown left the worker's pipe open"
        with ext_mod._POOL_LOCK:
            assert ext_mod._DOC_POOL is None, "the document pool survived shutdown"


def _counted_spawns(monkeypatch):
    """Count pool worker constructions parent-side (spawn-safe: no pickling)."""
    import app.crawl.extraction as ext_mod

    real_spawn = ext_mod._spawn_doc_worker
    constructions = 0

    def counting_spawn():
        nonlocal constructions
        constructions += 1
        return real_spawn()

    monkeypatch.setattr(ext_mod, "_spawn_doc_worker", counting_spawn)
    return lambda: constructions


def _use_single_worker(monkeypatch):
    import app.crawl.extraction as ext_mod

    monkeypatch.setenv("CRAWLUTRON_EXTRACTION_DOC_WORKERS", "1")
    monkeypatch.setenv("CRAWLUTRON_TEST_HOOKS", "1")
    monkeypatch.setenv("TEST_MARKITDOWN_OUTPUT", "## Hello Markdown")
    ext_mod._reset_doc_pool()


def _restore_pool():
    import app.crawl.extraction as ext_mod

    for var in ("TEST_MARKITDOWN_OUTPUT", "TEST_MARKITDOWN_HANG", "TEST_MARKITDOWN_DIE"):
        ext_mod.os.environ.pop(var, None)
    ext_mod.os.environ.pop("CRAWLUTRON_TEST_HOOKS", None)
    ext_mod.os.environ.pop("CRAWLUTRON_EXTRACTION_DOC_WORKERS", None)
    ext_mod._reset_doc_pool()


def test_doc_workers_persist_across_documents(monkeypatch):
    """N documents cost one worker construction, not N spawns."""
    import app.crawl.extraction as ext_mod

    constructions = _counted_spawns(monkeypatch)
    _use_single_worker(monkeypatch)
    try:
        for _ in range(3):
            result = ext_mod.extract_document(b"%PDF-1.4 fake", "http://example.com/x.pdf")
            assert result.markdown == "## Hello Markdown"
        assert constructions() == 1
        assert len(ext_mod._doc_pool_pids()) == 1
    finally:
        _restore_pool()


def test_doc_worker_pids_stable_across_tasks(monkeypatch):
    """The same worker serves consecutive documents (no respawn churn)."""
    import app.crawl.extraction as ext_mod

    _use_single_worker(monkeypatch)
    try:
        ext_mod.extract_document(b"%PDF-1.4 fake", "http://example.com/a.pdf")
        first = ext_mod._doc_pool_pids()
        ext_mod.extract_document(b"%PDF-1.4 fake", "http://example.com/b.pdf")
        second = ext_mod._doc_pool_pids()
        assert first == second
        assert all(pid > 0 for pid in first)
    finally:
        _restore_pool()


def test_doc_timeout_kills_and_pool_recovers(monkeypatch):
    """A hung worker is killed on timeout; the pool serves the next document."""
    import app.crawl.extraction as ext_mod

    constructions = _counted_spawns(monkeypatch)
    _use_single_worker(monkeypatch)
    try:
        import time

        monkeypatch.setenv("TEST_MARKITDOWN_HANG", "1")
        started = time.monotonic()
        result = ext_mod.extract_document(
            b"%PDF-1.4 fake",
            "http://example.com/hang.pdf",
            settings=settings_with(extraction_doc_timeout=0.05),
        )
        assert time.monotonic() - started < 5.0
        assert result.text is None and result.markdown is None
        hung_pids = ext_mod._doc_pool_pids()

        monkeypatch.delenv("TEST_MARKITDOWN_HANG")
        # A normal deadline for recovery: the respawned worker must boot (spawn
        # platforms) before the task timeout starts mattering again.
        recovered = ext_mod.extract_document(
            b"%PDF-1.4 fake",
            "http://example.com/ok.pdf",
            settings=settings_with(extraction_doc_timeout=10.0),
        )
        assert recovered.markdown == "## Hello Markdown"
        # The hung worker was killed and its slot respawned: exactly one
        # replacement construction, pool back at one live worker.
        assert constructions() == 2
        live_pids = ext_mod._doc_pool_pids()
        assert len(live_pids) == 1
        assert live_pids != hung_pids
    finally:
        _restore_pool()


def test_undead_retired_slot_is_reaped_from_the_pool(fake_pool, monkeypatch):
    """-D: a retired slot whose process will not die is skipped by both
    the handout scan (needs ``not retired``) and the respawn scan (needs
    ``not is_alive``), so nothing can ever recover it. Leaving it in the pool
    means it occupies a list slot -- and holds a PID -- forever while the size
    budget grows around it. It must be reaped on the normal path, exactly like
    full teardown does, so the pool and the budget stay consistent.
    """
    zombie = install_unkillable_doc_worker()

    spawned, worker = _acquire_the_replacement(fake_pool, 31337)

    assert worker is spawned[0], "the pool did not grow past the unkillable slot"
    assert zombie not in (ext_mod._DOC_POOL or []), (
        "the retired-but-undead slot was left in the pool: it can never be "
        "recovered and will occupy a slot for the life of the process"
    )


def test_a_retired_dead_slot_is_also_reaped_not_left_as_a_placeholder(fake_pool, monkeypatch):
    """The reap is not conditional on the process being undead: a slot that was
    retired after a normal timeout (process died and was reaped) must not
    linger either. The pool and the budget stay consistent only when no retired
    slot remains, so a later acquire sees ``len(pool) == usable``."""
    retired = _stub_worker(2323)
    retired.retired = True
    retired.busy = False
    with ext_mod._POOL_LOCK:
        ext_mod._DOC_POOL = [retired]

    spawned, worker = _acquire_the_replacement(fake_pool, 4242)

    assert worker is spawned[0]
    assert retired not in (ext_mod._DOC_POOL or []), (
        "a retired slot was left in the pool as a placeholder instead of reaped"
    )
    assert all(not each.retired for each in ext_mod._DOC_POOL or []), (
        "the pool holds a retired slot: the pool size and the usable budget have drifted apart"
    )
