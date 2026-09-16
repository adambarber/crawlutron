"""P0-4: the document pool must reap stubborn workers and never wedge.

Three related defects in ``app/crawl/extraction.py``:

1. ``_terminate_doc_process`` sent SIGTERM and joined once. A markitdown worker
   stuck in an uninterruptible native call (or with a handler installed by the
   library it is converting) ignores SIGTERM, so ``is_alive()`` never flipped:
   the child kept its pipe and its memory for the life of the process.
2. That same half-dead worker poisoned its slot. ``_acquire_doc_worker`` hands
   out any slot that is idle and ``is_alive()`` -- which a SIGTERM survivor
   truthfully reports -- so the pool kept serving a worker that could never
   answer again, and the lazy-respawn branch (``not is_alive()``) could not
   reach it either.
3. ``_POOL_LOCK.wait()`` had no deadline, so one busy flag that was never
   cleared (an exception escaping between acquire and the releasing ``finally``)
   stopped *every* later document extraction, forever. ``BrowserPool.acquire``
   is deadline-bounded; the document pool was not.

The stubbed process below is how the project simulates a child that ignores
SIGTERM (see ``_test_hooks_enabled``): a real stubborn child would need a
signal-blocking helper process to be planted in the suite.
"""

import threading
import time

import pytest

import app.crawl.extraction as ext_mod


class _StubProc:
    """``multiprocessing.Process`` stand-in with signal semantics.

    ``responds_to_sigterm=False`` models the runaway worker: SIGTERM is
    absorbed, only SIGKILL ends it. ``join`` records whether the child was
    already dead when it was called, so a test can tell "reaped" from "told to
    die and still running".
    """

    def __init__(self, pid: int = 4242, responds_to_sigterm: bool = True) -> None:
        self.pid = pid
        self.signals: list[str] = []
        self.join_calls: list[float | None] = []
        self.joined_after_kill = False
        self.closed_pipes = 0
        self._alive = True
        self._responds_to_sigterm = responds_to_sigterm

    def is_alive(self) -> bool:
        return self._alive

    def terminate(self) -> None:
        self.signals.append("SIGTERM")
        if self._responds_to_sigterm:
            self._alive = False

    def kill(self) -> None:
        self.signals.append("SIGKILL")
        self._alive = False

    def join(self, timeout=None) -> None:
        self.join_calls.append(timeout)
        if "SIGKILL" in self.signals:
            self.joined_after_kill = True

    @property
    def exitcode(self) -> int | None:
        return None if self._alive else -9


class _StubConn:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def send(self, payload) -> None:  # pragma: no cover - pool never used live
        raise AssertionError("stub worker must not serve a task")

    def poll(self, timeout) -> bool:  # pragma: no cover
        return False


def _stub_worker(pid: int, responds_to_sigterm: bool = True) -> ext_mod._DocWorker:
    worker = ext_mod._DocWorker(_StubProc(pid, responds_to_sigterm), _StubConn())
    worker.busy = False
    return worker


@pytest.fixture
def fake_pool(monkeypatch):
    """Give the pool stub slots, one worker wide, and restore it afterwards."""
    monkeypatch.setenv("BROWSER_CRAWLER_EXTRACTION_DOC_WORKERS", "1")
    monkeypatch.setenv("BROWSER_CRAWLER_TEST_HOOKS", "1")
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


def _acquire_with_bound(monkeypatch, bound: float = 0.25):
    """Call ``_acquire_doc_worker`` under a small acquire bound, in a thread.

    Returns ``(returned, still_blocked, elapsed)``. ``raising=False`` so this
    scenario also runs against un-bounded code, where the defect is the wedge
    itself rather than a missing knob; the thread is not a convenience: without
    a deadline the call never returns and the test would hang instead of
    reporting it.
    """
    monkeypatch.setattr(ext_mod, "_EXTRACTION_DOC_ACQUIRE_TIMEOUT", bound, raising=False)
    box: dict[str, object] = {}

    def _call() -> None:
        box["worker"] = ext_mod._acquire_doc_worker()

    thread = threading.Thread(target=_call, daemon=True)
    started = time.monotonic()
    thread.start()
    thread.join(timeout=bound * 20 + 5)
    return box.get("worker"), thread.is_alive(), time.monotonic() - started


# --- 1: SIGKILL escalation ---------------------------------------------------


def test_terminate_doc_process_escalates_to_sigkill_for_a_stubborn_worker():
    """A worker that absorbs SIGTERM must still be killed, then reaped."""
    proc = _StubProc(responds_to_sigterm=False)
    ext_mod._terminate_doc_process(proc)

    assert proc.signals[:1] == ["SIGTERM"], "graceful shutdown must be attempted first"
    assert "SIGKILL" in proc.signals, f"no escalation after SIGTERM was ignored: {proc.signals}"
    assert not proc.is_alive(), "runaway document worker survived termination"
    assert proc.joined_after_kill, "the killed worker was never reaped (zombie)"


def test_terminate_doc_process_does_not_kill_a_worker_that_exits_cleanly():
    """Inverse case: escalation is for survivors, not the normal path."""
    proc = _StubProc(responds_to_sigterm=True)
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

    replacement = _stub_worker(9999)
    spawned = []

    def _fake_spawn():
        spawned.append(True)
        return replacement

    fake_pool.setattr(ext_mod, "_spawn_doc_worker", _fake_spawn)

    worker = ext_mod._acquire_doc_worker()

    assert worker is replacement, f"acquire handed back a retired worker: {worker!r}"
    assert spawned, "the retired slot was not replaced"
    assert worker.busy is True, "a handed-out worker must be marked busy"


def test_healthy_idle_worker_is_reused_not_respawned(fake_pool):
    """Inverse case: the fix must not turn normal reuse into respawn churn."""
    live = _stub_worker(1234)
    with ext_mod._POOL_LOCK:
        ext_mod._DOC_POOL = [live]

    def _should_not_spawn():  # pragma: no cover - proves no respawn
        raise AssertionError("a live idle worker must be reused, not replaced")

    fake_pool.setattr(ext_mod, "_spawn_doc_worker", _should_not_spawn)

    assert ext_mod._acquire_doc_worker() is live


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

    worker, still_blocked, elapsed = _acquire_with_bound(fake_pool)

    assert not still_blocked, "_acquire_doc_worker waited forever on a stuck busy flag"
    assert worker is None, f"expected degradation to 'no worker', got {worker!r}"
    assert elapsed < 30.0, f"acquisition should degrade promptly, took {elapsed:.1f}s"


def test_acquire_returns_promptly_when_a_worker_is_released(fake_pool, real_sleeps):
    """The bound must not degrade the healthy path: a release wakes a waiter.

    ``_POOL_LOCK.wait(timeout)`` has to keep behaving like a condition wait --
    notified, not polled -- or every document pays the poll interval in latency.
    """
    busy = _stub_worker(5555)
    busy.busy = True
    with ext_mod._POOL_LOCK:
        ext_mod._DOC_POOL = [busy]

    # Generous bound: this test would also pass if the waiter simply ran to the
    # deadline, so the timing assertion below is what proves the notify woke it.
    box: dict[str, object] = {}

    def _call() -> None:
        box["worker"] = ext_mod._acquire_doc_worker()

    thread = threading.Thread(target=_call, daemon=True)
    thread.start()
    try:
        time.sleep(0.2)
        assert not box, "waiter returned before a worker was released"
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
    assert ext_mod._EXTRACTION_DOC_ACQUIRE_TIMEOUT > 0
    assert ext_mod._EXTRACTION_DOC_ACQUIRE_TIMEOUT < 3600


def test_extract_document_degrades_when_no_worker_can_be_acquired(fake_pool, monkeypatch):
    """Caller-visible half: no worker means empty content, not a crash or a hang.

    ``_extract_document`` used to assume acquire always succeeded; with a
    bounded acquire it must handle ``None`` the way it handles every other
    document failure -- empty extraction, logged.
    """
    stuck = _stub_worker(8888)
    stuck.busy = True
    with ext_mod._POOL_LOCK:
        ext_mod._DOC_POOL = [stuck]
    monkeypatch.setattr(ext_mod, "_EXTRACTION_DOC_ACQUIRE_TIMEOUT", 0.25, raising=False)

    result_box: dict[str, object] = {}

    def _call() -> None:
        result_box["result"] = ext_mod._extract_document(b"%PDF-1.4 fake", "http://example.com/x.pdf")

    thread = threading.Thread(target=_call, daemon=True)
    thread.start()
    thread.join(timeout=20)

    assert not thread.is_alive(), "_extract_document wedged waiting for a worker"
    result = result_box["result"]
    assert result.text is None and result.markdown is None


# --- 4/5/6: D5 acquire refinements ---------------------------------------


class _LockWatcherProc(_StubProc):
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


def test_acquire_budget_starts_after_the_lock_not_before_it(fake_pool, real_sleeps):
    """D5(a): time spent queued on ``_POOL_LOCK`` is not acquire budget.

    A caller blocked behind a slow in-lock operation must not find its whole
    budget already spent when it finally enters -- it would then report "no
    worker" for a pool that had a usable slot freed the entire time it waited.
    The deadline is therefore computed after the lock is acquired.
    """
    bound = 0.5
    fake_pool.setattr(ext_mod, "_EXTRACTION_DOC_ACQUIRE_TIMEOUT", bound, raising=False)
    worker = _stub_worker(6060)
    worker.busy = True
    with ext_mod._POOL_LOCK:
        ext_mod._DOC_POOL = [worker]

    box: dict[str, object] = {}

    def _acquire() -> None:
        box["worker"] = ext_mod._acquire_doc_worker()

    holder_ready = threading.Event()
    holder_done = threading.Event()

    def _hold_then_free() -> None:
        # Hold the pool lock past the acquirer's bound: this is the
        # blocked-behind-a-slow-retirement window. The slot is deliberately
        # STILL busy at release, so an acquirer that enters must wait.
        with ext_mod._POOL_LOCK:
            holder_ready.set()
            time.sleep(bound * 2)
        # Let the acquirer enter and (post-fix) park on the condition, then
        # free the slot and notify, exactly like a real release would.
        time.sleep(bound / 2)
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
    """D5(b): the replaced worker's kill-escalation must not hold the pool lock.

    The retirement is what can take ~10s; the slot swap itself is instant. The
    pool lock only needs to cover the swap, so the termination is deferred until
    after it. A probe in ``terminate`` proves the lock was free at that moment.
    """
    dead = ext_mod._DocWorker(_LockWatcherProc(alive=False), _StubConn())
    dead.busy = False
    with ext_mod._POOL_LOCK:
        ext_mod._DOC_POOL = [dead]

    replacement = _stub_worker(9090)

    def _fake_spawn():
        return replacement

    fake_pool.setattr(ext_mod, "_spawn_doc_worker", _fake_spawn)

    worker = ext_mod._acquire_doc_worker()

    assert worker is replacement, "the dead slot was not respawned"
    assert worker.busy is True
    assert dead.retired is True, "the replaced worker was never marked retired"
    assert dead.proc.lock_held_during_terminate == [False], (
        "the replaced worker was terminated while holding _POOL_LOCK; the "
        "kill-escalation would block every other acquire and release"
    )


def test_undead_retired_slot_does_not_consume_a_pool_slot(fake_pool, monkeypatch):
    """D5(c): a slot that will not die must not permanently shrink the pool.

    A retired-but-still-``is_alive()`` slot is skipped by BOTH scans (handout
    requires ``not retired``; respawn requires ``not is_alive``), so nothing in
    the normal path can ever recover it. If it still counts toward the size
    budget, the pool is one worker short forever and every later acquire
    degrades to "no worker" instead of growing past the zombie slot.
    """
    # responds_to_sigterm=False models a worker nothing can kill: is_alive()
    # stays True for the life of the process, and nothing here terminates it.
    zombie = ext_mod._DocWorker(_StubProc(1111, responds_to_sigterm=False), _StubConn())
    zombie.retired = True
    zombie.busy = False
    with ext_mod._POOL_LOCK:
        ext_mod._DOC_POOL = [zombie]

    replacement = _stub_worker(31337)
    spawned = []

    def _fake_spawn():
        spawned.append(True)
        return replacement

    fake_pool.setattr(ext_mod, "_spawn_doc_worker", _fake_spawn)
    monkeypatch.setattr(ext_mod, "_EXTRACTION_DOC_ACQUIRE_TIMEOUT", 0.3, raising=False)

    worker = ext_mod._acquire_doc_worker()

    assert spawned, "the pool did not grow past the unkillable slot"
    assert worker is replacement, (
        "acquire gave up instead of growing: an unkillable slot permanently "
        "consumed a pool-size budget"
    )
