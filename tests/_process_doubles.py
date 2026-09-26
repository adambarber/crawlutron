"""``multiprocessing.Process`` stand-ins for the document-extraction pool.

The pool's whole contract is signal semantics -- SIGTERM absorbed, SIGKILL
fatal, join after death, exitcode once dead -- so one double models all of it
and a test states only the property it needs (``responds_to_sigterm``,
``alive``). Two copies of this class were the duplication: a second one that
recorded shutdown, and a third shape for a worker nothing can kill, each
freezing its own idea of what ``exitcode`` should be.
"""

from typing import Any


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
        self.closed_pipes = 0
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


def unkillable_doc_worker(pid: int = 1111) -> Any:
    """A retired-but-still-``is_alive()`` pool slot nothing can reclaim.

    ``responds_to_sigterm=False`` models a worker nothing can kill: ``is_alive()``
    stays True for the life of the process, and no normal path terminates it.
    The pool must skip such a slot rather than let it hold a list slot and a PID
    forever.
    """
    import app.crawl.extraction as ext_mod

    zombie = ext_mod._DocWorker(StubProc(pid, responds_to_sigterm=False), StubConn())
    zombie.retired = True
    zombie.busy = False
    return zombie


def install_unkillable_doc_worker() -> Any:
    """:func:`unkillable_doc_worker`, seeded into the pool as its only slot."""
    import app.crawl.extraction as ext_mod

    zombie = unkillable_doc_worker()
    with ext_mod._POOL_LOCK:
        ext_mod._DOC_POOL = [zombie]
    return zombie
