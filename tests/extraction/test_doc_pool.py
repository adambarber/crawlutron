"""The persistent markitdown worker pool, asserted through its own constructor.

Every test builds a pool over stub workers instead of writing the slot list;
an arrangement a test can build is one production can reach.
"""

import threading
import time

import app.extraction.content as ext_mod
import app.extraction.documents as docex
from app.extraction.content import ExtractedContent
from app.extraction.doc_pool import (
    DocExtractionPool,
    DocWorker,
    DocWorkerSpawnError,
    terminate_doc_process,
)
from app.runtime.metrics import MetricsStore
from tests._doc_pool_fixtures import _seeded
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


def _recording_lock(lock, acquirer_thread: dict[str, int], acquirer_inside: threading.Event):
    """A pool lock that flags when the acquiring thread gets in."""

    class _RecordingLock(DelegatingLock):
        def acquire(self, *args, **kwargs):
            got = super().acquire(*args, **kwargs)
            if threading.get_ident() == acquirer_thread.get("id"):
                acquirer_inside.set()
            return got

    return _RecordingLock(lock)


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


def _failing_spawn_pool() -> DocExtractionPool:
    """An empty pool whose every spawn raises OSError (an unforkable host)."""

    def failing_spawn(ready_timeout: float) -> DocWorker:
        raise OSError("Resource temporarily unavailable")

    return DocExtractionPool(spawn=failing_spawn, worker_count=lambda: 1, enabled=lambda: True)


def _never_ready_spawn_pool() -> DocExtractionPool:
    """A pool whose every spawn reports a worker that never became ready."""

    def never_ready_spawn(ready_timeout: float) -> DocWorker:
        raise DocWorkerSpawnError("worker did not become ready")

    return DocExtractionPool(spawn=never_ready_spawn, worker_count=lambda: 1, enabled=lambda: True)


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


def test_acquire_after_teardown_does_not_respawn(fake_pool):
    """Teardown is terminal: a late acquire returns ``None`` (the degraded answer),
    never re-spawning the subprocesses shutdown just reaped (pins #246)."""
    live = stub_worker(4242)
    pool, spawner = _seeded(fake_pool, live)

    pool.teardown()
    got = pool.acquire(settings_with().extraction_doc_acquire_timeout)

    assert got is None, f"acquire resurrected the pool after teardown: {got!r}"
    assert spawner.asked == [], "teardown was not terminal: acquire re-spawned a worker"
    assert len(pool) == 0


def test_parked_acquire_wakes_to_none_after_teardown(fake_pool):
    """A thread parked in acquire when teardown runs must wake to ``None``, not a
    TypeError iterating the now-``None`` slot list (pins #254)."""
    busy = stub_worker(5555)
    box: dict[str, object] = {"error": None, "worker": object()}
    parked = threading.Event()

    class _ParkedCondition(threading.Condition):
        """The pool's lock, signaling when an acquirer reaches the park point.

        The seeding acquires never park (their spawn succeeds), so the event
        fires exactly when the test's acquirer enters the condition wait."""

        def wait(self, timeout=None):
            parked.set()
            return super().wait(timeout=timeout)

    pool, _spawner = _seeded(fake_pool, busy, lock=_ParkedCondition(threading.Lock()))
    busy.busy = True  # the only slot is busy, so the acquirer parks in the wait.

    def _call() -> None:
        try:
            box["worker"] = pool.acquire(60)
        except Exception as exc:  # pragma: no cover - a TypeError here fails the test
            box["error"] = exc
        finally:
            parked.set()

    thread = threading.Thread(target=_call, daemon=True)
    thread.start()
    # The event, not a sleep guess: teardown may run once the acquirer is parked.
    assert parked.wait(timeout=10), "the acquirer never reached the condition wait"
    pool.teardown()
    thread.join(timeout=10)

    assert box["error"] is None, f"parked acquirer escaped as {box['error']!r}"
    assert box["worker"] is None


def test_acquire_returns_none_when_spawn_fails():
    """A spawn failure (e.g. OSError on fork at the cgroup ceiling) degrades to
    ``None`` per the contract, never raising out of acquire (pins #269)."""
    pool = _failing_spawn_pool()
    worker = pool.acquire(0.1)

    assert worker is None


def test_a_worker_that_never_readies_degrades_the_acquisition():
    """A worker that never reports ready degrades the acquisition, not the document.

    The failure belongs to the pool: the caller's conversion budget must not
    be spent discovering that the worker never started."""
    pool = _never_ready_spawn_pool()
    worker = pool.acquire(0.1)

    assert worker is None


def test_the_spawn_is_bounded_by_the_remaining_acquisition_budget():
    """A cold worker's start-up is paid by the acquisition, not the conversion.

    The pool hands its spawner the budget it has left, so the child's import
    can never eat into the caller's conversion budget."""
    seen: list[float] = []

    def recording_spawn(ready_timeout: float) -> DocWorker:
        seen.append(ready_timeout)
        return stub_worker(1)

    pool = DocExtractionPool(spawn=recording_spawn, worker_count=lambda: 1, enabled=lambda: True)

    pool.acquire(5.0)

    assert seen, "the pool spawned without telling the spawner its budget"
    assert 0 < seen[0] <= 5.0, f"the spawn budget {seen[0]} is not the acquisition's remainder"


def test_extract_document_degrades_on_spawn_failure(monkeypatch):
    """A failed spawn surfaces from extract_document as the degraded empty result,
    never as an unhandled raise in the crawl path (pins #269)."""
    pool = _failing_spawn_pool()
    monkeypatch.setattr(docex, "DOC_POOL", pool)

    result = ext_mod.extract_document(b"%PDF-1.4 fake", "http://example.com/x.pdf")

    assert result.text is None and result.markdown is None


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


def test_a_starved_acquire_increments_the_doc_pool_unavailable_counter(fake_pool, monkeypatch):
    """A degrade is counted, not just logged (#346): the wedge alert must exist.

    The pool's only slot is busy, so a second acquisition runs out of budget
    and must land exactly one increment on the counter and the exposition."""
    stuck = stub_worker(4242)
    pool, _spawner = _seeded(fake_pool, stuck)
    stuck.busy = True
    store = MetricsStore()
    monkeypatch.setattr("app.runtime.metrics.metrics", store)

    assert pool.acquire(0.05) is None, "the acquisition did not degrade on the busy pool"
    assert store.snapshot().extraction_doc_pool_unavailable_total == 1.0
    assert "crawler_extraction_doc_pool_unavailable_total 1.0" in store.as_prometheus(caller_detail=False)


def test_a_failed_spawn_increments_the_doc_pool_unavailable_counter(monkeypatch):
    """A spawn that never became ready ends the acquisition as a degrade (#346):
    the counter must count it with the timeout path, or the wedge undercount."""

    def failing_spawn(ready_timeout: float) -> DocWorker:
        raise DocWorkerSpawnError("worker did not become ready")

    pool = DocExtractionPool(spawn=failing_spawn, worker_count=lambda: 1, enabled=lambda: True)
    store = MetricsStore()
    monkeypatch.setattr("app.runtime.metrics.metrics", store)

    assert pool.acquire(0.05) is None, "the acquisition did not degrade on the failed spawn"
    assert store.snapshot().extraction_doc_pool_unavailable_total == 1.0


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

    recording_lock = _recording_lock(pool_lock, acquirer_thread, acquirer_inside)
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
