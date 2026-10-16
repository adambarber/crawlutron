"""The persistent markitdown worker pool, asserted through its own constructor.

Every test builds a pool over stub workers instead of writing the slot list;
an arrangement a test can build is one production can reach.
"""

import threading
import time

import app.extraction.content as ext_mod
from app.extraction.content import ExtractedContent
from app.orchestration.doc_pool import DocExtractionPool, DocWorker, terminate_doc_process
from tests._extraction_helpers import settings_with
from tests._lock_doubles import DelegatingLock
from tests._process_doubles import (
    StubConn,
    StubProc,
    StubSpawner,
    install_unkillable_doc_worker,
    stub_proc,
    stub_worker,
)
from tests.crawl._doc_pool_fixtures import _seeded


def _acquire_the_replacement(pool: DocExtractionPool, spawner: StubSpawner, pid: int, bound: float = 0.3):
    """Acquire from a pool whose only slots are unusable; report the spawn too.

    Three tests ask the same grow-past-a-bad-slot question; the short bound is
    part of the arrangement, not a detail. Returns ``(spawned, worker)``."""
    spawner.hand_out(stub_worker(pid))
    worker = pool.acquire(bound)
    return spawner.asked, worker


def _acquire_with_bound(pool: DocExtractionPool, bound: float = 0.25):
    """Call ``pool.acquire`` under a small bound, in a thread.

    Returns ``(returned, still_blocked, elapsed)``; without a deadline the
    call never returns and the test would hang instead of reporting it."""
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

    A retired worker that survives SIGTERM still reports ``is_alive()`` and the
    old acquire loop served it to the next caller, blocking on a dead pipe."""
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

    At configured size with the only slot marked busy, acquisition must
    return "no worker" instead of blocking forever."""
    stuck = stub_worker(7777)
    pool, _spawner = _seeded(fake_pool, stuck)
    stuck.busy = True  # the holder never comes back for it

    worker, still_blocked, elapsed = _acquire_with_bound(pool)

    assert not still_blocked, "acquire waited forever on a stuck busy flag"
    assert worker is None, f"expected degradation to 'no worker', got {worker!r}"
    assert elapsed < 30.0, f"acquisition should degrade promptly, took {elapsed:.1f}s"


def test_acquire_returns_promptly_when_a_worker_is_released(fake_pool):
    """The bound must not degrade the healthy path: a release wakes a waiter.

    The condition wait must stay notified, not polled, and the ordering is a
    handshake: the waiter announces, then the slot frees."""
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
    """Caller-visible half: no worker means empty content, not a crash.

    With a bounded acquire, ``extract_document`` handles ``None`` like any
    other document failure: empty extraction, logged."""
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

    The kill-escalation runs long, so it must happen off-lock; ``terminate`` is
    its first slow step, which is where the lock state is observed.
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

    The deadline is computed after the lock is acquired, or a caller blocked
    behind a slow in-lock operation reports "no worker" for a usable pool."""
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

    The retirement is what can take ~10s; the pool lock only covers the
    instant slot swap, so termination is deferred until after it."""
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

    A retired-but-alive slot is skipped by both scans, so if it still counts
    toward the size budget the pool is one worker short forever."""
    _zombie, pool, spawner = install_unkillable_doc_worker()

    spawned, worker = _acquire_the_replacement(pool, spawner, 31337)

    assert spawned, "the pool did not grow past the unkillable slot"
    assert worker is spawned[0], (
        "acquire gave up instead of growing: an unkillable slot permanently consumed a pool-size budget"
    )


async def _noop() -> None:
    return None
