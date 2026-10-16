"""``multiprocessing.Process`` stand-ins for the document-extraction pool.

One double models the whole signal contract (SIGTERM absorbed, SIGKILL fatal,
join after death); three copies previously froze three ideas of ``exitcode``."""

from typing import Any

from app.orchestration.doc_pool import DocExtractionPool, DocWorker


class StubProc:
    """A ``multiprocessing.Process`` stand-in with signal semantics.

    ``responds_to_sigterm=False`` models the runaway worker (only SIGKILL ends
    it); ``join`` also records prior death ("reaped" vs "still running")."""

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
    """A retired-but-``is_alive()`` pool slot nothing can reclaim.

    ``responds_to_sigterm=False`` keeps ``is_alive()`` True for the process's
    life; the pool must skip the slot rather than let it hold a PID forever."""
    zombie = DocWorker(StubProc(pid, responds_to_sigterm=False), StubConn())
    zombie.retired = True
    zombie.busy = False
    return zombie


class StubSpawner:
    """A pool ``spawn`` that hands out known workers and records that it asked.

    Asserting only the worker would pass a pool that grew its slot another way;
    ``clear()`` retires a seeding so the record speaks about the test."""

    def __init__(self, *workers: DocWorker) -> None:
        self._queue = list(workers)
        self._last: DocWorker | None = None
        self.asked: list[DocWorker] = []

    def __call__(self) -> DocWorker:
        """The next queued worker, or the last one out once the queue runs dry.

        Repeating rather than raising is what a pool growing one slot at a time
        needs: a second spawn respawns the same slot."""
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

    Grown through a real acquisition, so the pool matches production's arrival;
    the spawner lets a test queue the workers the pool should grow into."""
    import app.extraction.content as ext_mod

    zombie = unkillable_doc_worker()
    spawner = StubSpawner(zombie)
    pool = DocExtractionPool(spawn=spawner, worker_count=lambda: 1, enabled=lambda: True)
    pool.acquire(0.1)
    pool.release(zombie)
    spawner.asked.clear()
    ext_mod.DOC_POOL = pool
    return zombie, pool, spawner
