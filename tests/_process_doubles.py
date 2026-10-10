"""``multiprocessing.Process`` stand-ins for the document-extraction pool.

The pool's whole contract is signal semantics -- SIGTERM absorbed, SIGKILL
fatal, join after death, exitcode once dead -- so one double models all of it
and a test states only the property it needs (``responds_to_sigterm``,
``alive``). Two copies of this class were the duplication: a second one that
recorded shutdown, and a third shape for a worker nothing can kill, each
freezing its own idea of what ``exitcode`` should be.
"""

from typing import Any

from app.orchestration.doc_pool import DocExtractionPool, DocWorker


class StubProc:
    """A ``multiprocessing.Process`` stand-in with signal semantics.

    ``responds_to_sigterm=False`` models the runaway worker: SIGTERM is
    absorbed, only SIGKILL ends it. ``alive=False`` models a child that is
    already dead. ``join`` records the timeout it was given and whether the
    child was already dead when it was called, so a test can tell "reaped"
    from "told to die and still running".
    """

    def __init__(self, pid: int = 4242, responds_to_sigterm: bool = True, alive: bool = True) -> None:
        self.pid = pid
        self.signals: list[str] = []
        self.join_calls: list[float | None] = []
        self.joined_after_kill = False
        self._alive = alive
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
        # The real worker join accepts an optional timeout; the stand-in records it.
        self.join_calls.append(timeout)
        if "SIGKILL" in self.signals:
            self.joined_after_kill = True

    @property
    def exitcode(self) -> int | None:
        return None if self._alive else -SIGKILL_STATUS

    @property
    def terminated(self) -> bool:
        """Whether anything was ever signalled -- the teardown-was-reached probe."""
        return bool(self.signals)


# SIGKILL's reported wait status, which is what a killed child exits with.
SIGKILL_STATUS = 9


class StubConn:
    """The worker end of the task pipe, with no task to serve."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def send(self, payload) -> None:
        raise AssertionError("stub worker must not serve a task")

    def poll(self, timeout) -> bool:
        return False


def stub_proc(pid: int = 4242, *, responds_to_sigterm: bool = True, alive: bool = True) -> StubProc:
    return StubProc(pid, responds_to_sigterm, alive)


def stub_worker(pid: int, responds_to_sigterm: bool = True) -> DocWorker:
    """An idle :class:`DocWorker` over :class:`StubProc` and :class:`StubConn`.

    Shared because three modules need the same pairing, and a second copy of it
    would be a second idea of what a worker awaiting hand-out looks like.
    """
    worker = DocWorker(StubProc(pid, responds_to_sigterm), StubConn())
    worker.busy = False
    return worker


def unkillable_doc_worker(pid: int = 1111) -> Any:
    """A retired-but-still-``is_alive()`` pool slot nothing can reclaim.

    ``responds_to_sigterm=False`` models a worker nothing can kill: ``is_alive()``
    stays True for the life of the process, and no normal path terminates it.
    The pool must skip such a slot rather than let it hold a list slot and a PID
    forever.
    """
    zombie = DocWorker(StubProc(pid, responds_to_sigterm=False), StubConn())
    zombie.retired = True
    zombie.busy = False
    return zombie


class StubSpawner:
    """A pool ``spawn`` that hands out known workers and records that it asked.

    Asserting only that a handed-out worker is the expected one would pass on a
    pool that grew its slot some other way, so the record is asserted too: it is
    empty until the pool actually asks, and ``clear()`` retires a seeding so the
    record speaks only about what the test's own acquisition did.
    """

    def __init__(self, *workers: DocWorker) -> None:
        self._queue = list(workers)
        self._last: DocWorker | None = None
        self.asked: list[DocWorker] = []

    def __call__(self) -> DocWorker:
        """The next queued worker, or the last one handed out once the queue runs dry.

        Repeating rather than raising is what a pool that grows one slot at a time
        needs: a second spawn in the same test is a respawn of the same slot, and
        a double that raised would be asserting a fact about the test rather than
        about the pool.
        """
        worker = self._queue.pop(0) if self._queue else self._last
        assert worker is not None, "a spawn with nothing queued and nothing handed out"
        self._last = worker
        self.asked.append(worker)
        return worker

    def hand_out(self, *workers: DocWorker) -> None:
        """Queue more workers for a later spawn."""
        self._queue.extend(workers)


def install_unkillable_doc_worker() -> tuple[DocWorker, DocExtractionPool, StubSpawner]:
    """:func:`unkillable_doc_worker`, seeded into the pool as its only slot.

    The pool is grown to hold the worker through a real acquisition, so the
    seeded pool was arrived at the way production arrives at one. The spawner
    comes back so a test can queue the workers the pool should grow into once it
    has reaped the slot nothing can reclaim.
    """
    import app.extraction.content as ext_mod

    zombie = unkillable_doc_worker()
    spawner = StubSpawner(zombie)
    pool = DocExtractionPool(spawn=spawner, worker_count=lambda: 1, enabled=lambda: True)
    pool.acquire(0.1)
    pool.release(zombie)
    spawner.asked.clear()
    ext_mod.DOC_POOL = pool
    return zombie, pool, spawner
