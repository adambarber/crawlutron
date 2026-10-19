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

from markitdown import MarkItDown

from app.config.env import parse_bool_env

logger = logging.getLogger(__name__)

# Named start method, never inherited: a forked child inherits the parent's
# loop, sockets and locks held for good, so one spawn context pairs every child.
WORKER_CONTEXT = multiprocessing.get_context("spawn")

# Re-check interval for a waiting acquirer (the escape hatch for "nothing will
# ever notify"); releases still wake waiters immediately via notify_all.
ACQUIRE_RECHECK_SECONDS = 1.0

# Grace period for a document worker to honor SIGTERM before we escalate.
TERMINATE_GRACE_SECONDS = 5.0


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


def terminate_doc_process(proc) -> None:
    """Terminate a runaway document-extraction process and reap it.
    SIGTERM alone is not enough: a wedged native conversion absorbs it and holds
    pipe/memory/slot for the service life, so escalate to SIGKILL and join lest a zombie lingers.
    """
    with suppress(Exception):
        proc.terminate()
    with suppress(Exception):
        proc.join(timeout=TERMINATE_GRACE_SECONDS)
    with suppress(Exception):
        if proc.is_alive():
            proc.kill()
            proc.join(timeout=TERMINATE_GRACE_SECONDS)


def test_hooks_enabled() -> bool:
    """Whether the ``TEST_MARKITDOWN_*`` child hooks are in force: they let tests
    simulate child death, hangs and output without real parsers, and fire only
    with ``CRAWLUTRON_TEST_HOOKS=1`` plus the per-hook flag -- a stray env must never alter production.
    """
    return parse_bool_env("CRAWLUTRON_TEST_HOOKS", default=False)


def doc_task_hooks() -> dict | None:
    """Snapshot the TEST_MARKITDOWN_* hooks for one task, parent-side.
    Persistent workers would otherwise act on spawn-time env, not the caller's
    current flags; the parent ships the snapshot with the task and the worker honors the message.
    """
    if not test_hooks_enabled():
        return None
    return {
        "die": parse_bool_env("TEST_MARKITDOWN_DIE", default=False),
        "hang": parse_bool_env("TEST_MARKITDOWN_HANG", default=False),
        "output": os.environ.get("TEST_MARKITDOWN_OUTPUT"),
    }


def configure_doc_worker_logging() -> None:
    """Quiet expected parser chatter in the document worker: a bare child has no
    service logging, and pdfminer warns per font whose descriptor lacks a usable
    FontBBox (common, no signal) -- drop it at the source, keep ERROR+.
    """
    logging.getLogger("pdfminer").setLevel(logging.ERROR)


def _markitdown_extract(body: bytes) -> str | None:
    """Convert document bytes with markitdown (worker-side real path only). Test
    hooks are NOT read here: the parent snapshots them per task, and the worker
    honors the shipped message, so persistent workers never observe stale env.
    """
    res = MarkItDown().convert_stream(io.BytesIO(body))
    return res.text_content if res is not None else None


def _doc_worker_main(conn) -> None:
    """Persistent worker entry: serve markitdown tasks until killed or orphaned.
    Each task arrives as ``(body, hooks)`` from the parent's per-call snapshot
    (never spawn-time env); a ``die`` hook exits without sending, like a segfault.
    """
    try:
        configure_doc_worker_logging()
        while True:
            try:
                body, hooks = conn.recv()
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
                        conn.send(("ok", hooks["output"]))
                    continue
            try:
                md = _markitdown_extract(body)
                conn.send(("ok", md))
            except Exception:
                with suppress(Exception):
                    conn.send(("err", None))
    finally:
        with suppress(Exception):
            conn.close()


def spawn_doc_worker() -> DocWorker:
    """Start one persistent worker and return its parent-side handle."""
    parent_conn, child_conn = WORKER_CONTEXT.Pipe(duplex=True)
    proc = WORKER_CONTEXT.Process(target=_doc_worker_main, args=(child_conn,), daemon=True)
    proc.start()
    child_conn.close()  # parent keeps only its end of the pipe
    return DocWorker(proc, parent_conn)


class DocExtractionPool:
    """The persistent document-worker pool, and the only thing that owns it.
    The builder, worker count, enabled flag and slot lock all arrive via the
    constructor, written once against collaborators; production builds one from the environment.
    """

    def __init__(
        self,
        *,
        spawn: Callable[[], DocWorker] = spawn_doc_worker,
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

    def _try_spawn(self) -> DocWorker | None:
        """Spawn a worker, or ``None`` when the process cannot fork (OSError).
        A spawn failure is transient (cgroup ceiling), distinct from a teardown (#269)."""
        try:
            return self._spawn()
        except OSError:
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
                    for worker in self._slots:
                        if not worker.busy and not worker.retired and worker.proc.is_alive():
                            worker.busy = True
                            return worker
                    for index, worker in enumerate(self._slots):
                        if not worker.busy and not worker.proc.is_alive():
                            if (fresh := self._try_spawn()) is None:
                                return None
                            fresh.busy = True
                            self._slots[index] = fresh
                            dead_slots.append(worker)
                            return fresh
                    # A short live set grows by one rather than degrading to no worker.
                    usable = sum(1 for each in self._slots if not each.retired)
                    if usable < want:
                        if (fresh := self._try_spawn()) is None:
                            return None
                        fresh.busy = True
                        self._slots.append(fresh)
                        return fresh
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        logger.warning(
                            "extraction_document_pool_unavailable",
                            extra={"slots": len(self._slots), "workers": want},
                        )
                        return None
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
        terminate_doc_process(worker.proc)
        with suppress(Exception):
            worker.conn.close()

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
