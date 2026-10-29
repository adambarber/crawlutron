"""The persistent markitdown worker pool, and nothing else about extraction.
Document extraction runs in a persistent worker pool (import once, amortize
spawn cost), with per-task timeout-kill; all collaborators arrive via the pool constructor.
"""

import io
import logging
import multiprocessing
import os
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from typing import TYPE_CHECKING, TypedDict

from app.runtime.metrics import get_metrics
from app.runtime.pool import SyncPool

logger = logging.getLogger(__name__)


def configure_doc_worker_runtime() -> None:
    """Quiet the native stack the worker carries, before any conversion runs."""
    try:
        import onnxruntime  # type: ignore[import-untyped]

        onnxruntime.disable_telemetry_events()
    except Exception:
        logging.getLogger(__name__).debug("onnxruntime telemetry disable unavailable")


# The doc-pool death-simulation seam: tests install a hooks payload here
# (a per-process holder, never env) and the worker honors what the parent ships.
class DocPoolHooks(TypedDict):
    """The child-simulation hooks one task may carry: die/hang/output."""

    die: bool
    hang: bool
    output: str | None


class _HooksHolder:
    __slots__ = ("active", "payload")

    def __init__(self) -> None:
        self.active = False
        self.payload: DocPoolHooks | None = None


doc_pool_hooks = _HooksHolder()


def install_doc_pool_hooks(hooks: DocPoolHooks | None) -> None:
    """Arm (or clear) the doc-pool death-simulation hooks for this process."""
    doc_pool_hooks.active = hooks is not None
    doc_pool_hooks.payload = hooks


# Named start method, never inherited: a forked child inherits the parent's
# loop, sockets and locks held for good, so one spawn context pairs every child.
WORKER_CONTEXT = multiprocessing.get_context("spawn")

# Re-check interval for a waiting acquirer (the escape hatch for "nothing will
# ever notify"); releases still wake waiters immediately via notify_all.
ACQUIRE_RECHECK_SECONDS = 1.0

# Grace period for a document worker to honor SIGTERM before we escalate.
TERMINATE_GRACE_SECONDS = 5.0

# A worker parked at ``recv()`` sees the pipe close and exits through full, clean
# teardown; the grace only has to cover noticing the EOF.
RETIRE_GRACE_SECONDS = 1.0

# How long a spawned worker may take to finish importing and report ready.
# The pool passes the caller's remaining acquisition budget instead.
SPAWN_READY_TIMEOUT = 30.0


class DocWorker:
    """One persistent markitdown worker: process + parent end of its task pipe."""

    __slots__ = ("proc", "conn", "busy", "retired")

    def __init__(self, proc, conn) -> None:
        self.proc = proc
        self.conn = conn
        self.busy = False
        # Aliveness is not proof of usability: termination is best-effort, so this
        # flag -- not the process state -- keeps a retired worker out of circulation.
        self.retired = False


def terminate_doc_process(proc, conn=None) -> None:
    """Terminate a runaway document-extraction process and reap it."""
    if conn is not None:  # graceful: parked worker exits on EOF, escalation on missed grace
        with suppress(Exception):
            conn.close()
        proc.join(timeout=RETIRE_GRACE_SECONDS)
    with suppress(Exception):
        proc.terminate()
    with suppress(Exception):
        proc.join(timeout=TERMINATE_GRACE_SECONDS)
    with suppress(Exception):
        if proc.is_alive():
            proc.kill()
            proc.join(timeout=TERMINATE_GRACE_SECONDS)


def test_hooks_enabled() -> bool:
    """Whether the doc-pool death-simulation hooks are wired in this process.
    Tests inject hooks directly; production carries no env gate at all.
    """
    return bool(getattr(doc_pool_hooks, "active", False))


def doc_task_hooks() -> DocPoolHooks | None:
    """The hooks for one task, parent-side: exactly what the seam injected.
    Persistent workers would otherwise act on spawn-time state, not the caller's
    current flags; the parent ships the snapshot with the task and the worker honors the message.
    """
    if not test_hooks_enabled():
        return None
    return doc_pool_hooks.payload


def configure_doc_worker_logging() -> None:
    """Quiet expected parser chatter in the document worker: a bare child has no
    service logging, and pdfminer warns per font whose descriptor lacks a usable
    FontBBox (common, no signal) -- drop it at the source, keep ERROR+.
    """
    logging.getLogger("pdfminer").setLevel(logging.ERROR)


def _markitdown_extract(body: bytes) -> str | None:
    """Convert document bytes with markitdown (worker-side real path only). Test
    hooks are NOT read here: the parent ships them per task, and the worker
    honors the shipped message; the parent never imports markitdown.
    """
    from markitdown import MarkItDown

    res = MarkItDown().convert_stream(io.BytesIO(body))
    return res.text_content if res is not None else None


def doc_worker_main(conn) -> None:
    """Persistent worker entry: serve markitdown tasks until killed or orphaned.
    Each task arrives as ``(body, hooks, cap)`` from the parent's per-call payload
    (never process-global state); a ``die`` hook exits without sending, like a segfault.
    """
    try:
        configure_doc_worker_runtime()
        configure_doc_worker_logging()
        # Warm import inside the readiness handshake: the worker
        # pays markitdown's cost here, inside the acquisition budget.
        import markitdown  # noqa: F401  imported for its cost, in the worker only

        # Readiness is signalled here, after the child's imports: the parent's
        # conversion budget must not pay for this worker's start-up.
        conn.send(("ready", None))
        while True:
            try:
                body, hooks, cap = conn.recv()
            except (EOFError, OSError):
                return
            if hooks is not None:
                if hooks.get("die"):
                    with suppress(Exception):
                        conn.close()
                    os._exit(1)
                if hooks.get("hang"):
                    time.sleep(10)  # Longer than any test timeout
                if hooks.get("output") is not None:
                    with suppress(Exception):
                        conn.send(("ok", hooks["output"][:cap]))
                    continue
            try:
                md = _markitdown_extract(body)
                conn.send(("ok", md[:cap] if md else md))
            except Exception:
                with suppress(Exception):
                    conn.send(("err", None))
    finally:
        with suppress(Exception):
            conn.close()
        # Clean pipe-EOF exit skips native teardown (os._exit), so onnxruntime's telemetry
        # WorkerThread never races module finalization into the recursive-mutex abort.
        os._exit(0)


class DocWorkerSpawnError(RuntimeError):
    """A worker that never became ready: it died, or it never finished importing."""


def spawn_doc_worker(ready_timeout: float = SPAWN_READY_TIMEOUT) -> DocWorker:
    """Start one persistent worker and wait until it can serve a task.

    The wait is bounded by the caller's acquisition budget: a cold worker pays
    for its own imports here, not inside the caller's conversion budget.
    """
    parent_conn, child_conn = WORKER_CONTEXT.Pipe(duplex=True)
    proc = WORKER_CONTEXT.Process(target=doc_worker_main, args=(child_conn,), daemon=True)
    proc.start()
    child_conn.close()  # parent keeps only its end of the pipe
    worker = DocWorker(proc, parent_conn)
    try:
        if not parent_conn.poll(ready_timeout):
            raise DocWorkerSpawnError("worker did not become ready")
        parent_conn.recv()
    except DocWorkerSpawnError:
        terminate_doc_process(proc)
        raise
    except (EOFError, OSError) as exc:
        terminate_doc_process(proc)
        raise DocWorkerSpawnError(str(exc)) from exc
    return worker


class DocExtractionPool:
    """The persistent document-worker pool, and the only thing that owns it.
    The builder, worker count, enabled flag and slot lock all arrive via the
    constructor, written once against collaborators; production builds one from the environment.
    """

    def __init__(
        self,
        *,
        spawn: Callable[[float], DocWorker] = spawn_doc_worker,
        worker_count: Callable[[], int],
        enabled: Callable[[], bool],
        recheck_seconds: float = ACQUIRE_RECHECK_SECONDS,
        lock: threading.Condition | None = None,
    ) -> None:
        self._spawn = spawn
        self._worker_count = worker_count
        self._enabled = enabled
        self._recheck_seconds = recheck_seconds
        # Injectable so a caller can observe or hold the lock itself, proving that
        # a budget starts only once the lock is held.
        self._lock = lock if lock is not None else threading.Condition(threading.Lock())
        self._slots: list[DocWorker] | None = None
        self._closed = False

    def _spawn_ready(self, ready_timeout: float) -> DocWorker:
        """A worker ready to serve, or the failure that ends the acquisition.

        A spawn failure is transient (cgroup ceiling) and distinct from a
        teardown; it ends this acquisition rather than being waited out."""
        try:
            return self._spawn(ready_timeout)
        except (OSError, DocWorkerSpawnError) as exc:
            get_metrics().record_doc_pool_unavailable()
            logger.warning(
                "extraction_document_spawn_failed",
                extra={"error": str(exc), "ready_timeout": ready_timeout},
            )
            raise DocWorkerSpawnError(str(exc)) from exc

    def _idle_worker(self) -> DocWorker | None:
        """A slot that is free, not retired, and still alive."""
        for worker in self._slots or []:
            if not worker.busy and not worker.retired and worker.proc.is_alive():
                return worker
        return None

    def _budget_left(self, deadline: float, want: int) -> bool:
        """Whether the acquisition budget still has time, and say so when it does not."""
        if deadline - time.monotonic() > 0:
            return True
        get_metrics().record_doc_pool_unavailable()
        logger.warning(
            "extraction_document_pool_unavailable",
            extra={"slots": len(self._slots or []), "workers": want},
        )
        return False

    def _grow(self, deadline: float, want: int) -> tuple[DocWorker, DocWorker | None] | None:
        """A replacement for a dead slot, or one more slot: (worker, replaced).

        ``None`` means the pool is the size it wants; a spawn that fails raises.
        """
        slots = self._slots
        if slots is None:
            return None
        for index, worker in enumerate(slots):
            if not worker.busy and not worker.proc.is_alive():
                fresh = self._spawn_ready(max(0.0, deadline - time.monotonic()))
                slots[index] = fresh
                return fresh, worker
        usable = sum(1 for each in slots if not each.retired)
        if usable < want:
            fresh = self._spawn_ready(max(0.0, deadline - time.monotonic()))
            slots.append(fresh)
            return fresh, None
        return None

    def acquire(self, timeout: float) -> DocWorker | None:
        """Take an idle pool worker, spawning on demand up to the configured size.
        Dead idle slots respawn in place; retired slots are never handed out, and
        ``None`` (no worker within timeout) means callers must degrade the document, not wait.
        """
        if not self._enabled():
            return None
        # Swapped under the lock, terminated after it: kill escalation takes ~10s
        # and would block every acquisition; the list drains in the finally below.
        dead_slots: list[DocWorker] = []
        try:
            with self._lock:
                if self._closed:
                    return None
                if self._slots is None:
                    self._slots = []
                # Reap retired slots first: one skipped by both scans strands a PID.
                self._slots = [each for each in self._slots if not each.retired]
                want = self._worker_count()
                # The budget starts once the lock is held: a queued caller must
                # not pay a lock wait out of a budget it never consumed.
                deadline = time.monotonic() + timeout
                while True:
                    if self._closed:  # a parked acquirer woken by teardown must see it here
                        return None
                    idle = self._idle_worker()
                    if idle is not None:
                        idle.busy = True
                        return idle
                    if not self._budget_left(deadline, want):
                        return None
                    try:
                        grown = self._grow(deadline, want)
                    except DocWorkerSpawnError:
                        return None
                    if grown is not None:
                        fresh, replaced = grown
                        fresh.busy = True
                        if replaced is not None:
                            dead_slots.append(replaced)
                        return fresh
                    if not self._budget_left(deadline, want):
                        return None
                    remaining = deadline - time.monotonic()
                    # A condition wait (not a poll): a release notifies immediately.
                    # The bounded re-check only guards a busy flag nothing clears.
                    self._lock.wait(timeout=min(remaining, self._recheck_seconds))
        finally:
            # Same retirement path as the timeout/child-death call sites: retire
            # marks retired, terminates and reaps.
            for worker in dead_slots:
                self.retire(worker)

    def retire(self, worker: DocWorker) -> None:
        """Terminate and reap one worker, leaving its (dead) slot for lazy respawn.
        The retired mark is set before anything slow -- retirement is not always under
        the lock -- so no acquisition ever hands a doomed worker to the next caller.
        """
        worker.retired = True
        terminate_doc_process(worker.proc, worker.conn)

    def release(self, worker: DocWorker) -> None:
        """Hand a slot back and wake everyone waiting for it."""
        with self._lock:
            # Clear busy even on a mid-task retirement: ``retired``, not ``busy``,
            # keeps the worker out of circulation; the notify wakes lazy respawn.
            worker.busy = False
            self._lock.notify_all()

    def pids(self) -> list[int]:
        """Live worker PIDs: proves persistence, not spawn-per-task."""
        with self._lock:
            return [worker.proc.pid for worker in self._slots or [] if worker.proc.pid]

    def health(self) -> list[tuple[bool, bool, bool]]:
        """Per-worker ``(alive, reaped, busy)`` snapshot.
        Idle workers legitimately stay alive across calls, so the contract is per worker,
        not process-global: a died worker must be reaped (not a zombie) and none left busy mid-task.
        """
        with self._lock:
            return [
                (worker.proc.is_alive(), worker.proc.exitcode is not None, worker.busy)
                for worker in self._slots or []
            ]

    def teardown(self) -> None:
        """Terminate every pooled worker, forget the slots, and close the pool.
        A later acquire returns ``None`` (degraded) instead of resurrecting it."""
        with self._lock:
            self._closed = True
            self._lock.notify_all()
            pool, self._slots = self._slots, None
            for worker in pool or []:
                self.retire(worker)

    def __len__(self) -> int:
        """How many slots the pool holds (none before the first acquisition)."""
        with self._lock:
            return len(self._slots or [])


if TYPE_CHECKING:
    # Static conformance, checked by mypy (the type gate): DocExtractionPool must
    # satisfy SyncPool[DocWorker]. A drift in acquire/release/__len__ fails the gate.
    _DOC_POOL_IS_A_SYNC_POOL: SyncPool[DocWorker] = DocExtractionPool(
        worker_count=lambda: 1, enabled=lambda: True
    )
