"""The persistent markitdown worker pool, and nothing else about extraction.

Document extraction (markitdown) runs in a PERSISTENT pool of worker processes.
Each worker imports markitdown once (it stays in the worker's ``sys.modules``)
and serves many documents, amortizing the spawn + import cost of the old
spawn-per-document design. Timeout-kill semantics are kept per task: a worker
that exceeds the document deadline -- or dies mid-task -- is terminated and
reaped immediately, and its slot is respawned on the next acquisition, so a
pathological document can neither block the pool nor linger as a zombie.

Everything the pool touches arrives through :class:`DocExtractionPool`'s
constructor: how a worker is built, how many there should be, whether the
feature is on, and the lock its slots live under. A caller that needs a
different arrangement builds a pool instead of rewriting another module's names,
which is what lets the suite test this machinery against stubs rather than by
reaching into ``app.crawl.extraction``'s internals.
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

from app.validation import parse_bool_env

logger = logging.getLogger(__name__)

# The start method is named, never inherited from the platform. The default is
# `fork` on Linux and `spawn` on macOS, and a forked extraction worker is a copy
# of the whole crawler: the running event loop, every open socket, the HTTP
# cache, and any lock another thread held at fork time -- held for good, since
# the thread that owned it does not exist in the child. This worker does nothing
# but CPU-bound markitdown over a pipe, so a clean interpreter is safer and no
# more costly, and the persistent pool exists to amortize the spawn cost that
# naming it does not remove.
#
# A forked child also freezes the parent's env at fork time, so a long-lived
# worker predating a setting never observed it -- which is how the timeout suite
# passed on macOS and failed on the same assertion on Linux, with no change to
# the code under test. Every process object handed to the child therefore comes
# from this one context: a pipe from the default context and a process from
# another is not a pairing that works.
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
        # True once the worker has been terminated. Termination is only
        # best-effort, so a retired process can still report ``is_alive()``;
        # "alive" is therefore not proof of "usable", and this flag -- not the
        # process state -- is what keeps a retired worker out of circulation.
        self.retired = False


def terminate_doc_process(proc) -> None:
    """Terminate a runaway document-extraction process and reap it.

    SIGTERM alone is not enough: a worker wedged in an uninterruptible native
    conversion absorbs it and holds its pipe, memory and pool slot for the life of
    the service. Escalate to SIGKILL, and join after the kill so the child cannot
    linger as a zombie.
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
    """Whether the ``TEST_MARKITDOWN_*`` child hooks are in force.

    They exist so tests can simulate child death, hangs and output without real
    parsers, and they only fire when the operator exports ``CRAWLUTRON_TEST_HOOKS=1``
    alongside the per-hook flag -- a stray env var must never alter production.
    """
    return parse_bool_env("CRAWLUTRON_TEST_HOOKS", default=False)


def doc_task_hooks() -> dict | None:
    """Snapshot the ``TEST_MARKITDOWN_*`` hooks for one task (parent-side).

    Workers are persistent, so they would otherwise act on the env from their spawn
    moment rather than the caller's current flags. The parent always sees current
    env, so it snapshots per call and ships the result with the task; the worker
    honors the message, never its own env.
    """
    if not test_hooks_enabled():
        return None
    return {
        "die": parse_bool_env("TEST_MARKITDOWN_DIE", default=False),
        "hang": parse_bool_env("TEST_MARKITDOWN_HANG", default=False),
        "output": os.environ.get("TEST_MARKITDOWN_OUTPUT"),
    }


def configure_doc_worker_logging() -> None:
    """Quiet expected parser chatter in the document worker.

    A bare child process has no service logging, so pdfminer warns per font whose
    descriptor lacks a usable FontBBox -- common on municipal PDFs, and carrying no
    signal when the text still parsed. Drop it at the source, keep ERROR+.
    """
    logging.getLogger("pdfminer").setLevel(logging.ERROR)


def _markitdown_extract(body: bytes) -> str | None:
    """Convert document bytes with markitdown (worker-side real path only).

    Test hooks are NOT read here: the parent snapshots them per task and the
    worker honors the shipped message, so persistent workers never observe stale
    env.
    """
    res = MarkItDown().convert_stream(io.BytesIO(body))
    return res.text_content if res is not None else None


def _doc_worker_main(conn) -> None:
    """Persistent worker entry: serve markitdown tasks until killed or orphaned.

    Each task arrives as ``(body, hooks)``; hooks come from the parent's per-call
    snapshot (never the worker's spawn-time env). A ``die`` hook exits without
    sending (a worker segfaulting on hostile input).
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

    How a worker is built, how many there should be, whether the feature is on and
    the lock the slots live under all arrive through the constructor, so the
    machinery below is written once against collaborators rather than module names.
    Production builds one from the environment; a caller needing a different
    arrangement builds its own.
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
        # Injectable so a caller can observe or hold the lock itself -- proving
        # that a budget starts only once the lock is held needs a lock it can see
        # from inside.
        self._lock = lock if lock is not None else threading.Condition(threading.Lock())
        self._slots: list[DocWorker] | None = None

    def acquire(self, timeout: float) -> DocWorker | None:
        """Take an idle pool worker, spawning on demand up to the configured size.

        The pool starts empty and grows only as demand requires, so a caller doing
        one extraction owns exactly one worker and the zero-live-children-after-death
        invariant still holds. Dead idle slots are respawned in place; retired slots
        are never handed out, alive-report or not.

        ``None`` means no worker within ``timeout``, and callers must degrade the
        document rather than wait: the alternative was a wait with no deadline, so
        one busy flag its holder never cleared wedged every later extraction in the
        process.
        """
        if not self._enabled():
            return None
        # Dead slots discovered here are replaced under the lock but TERMINATED
        # after it: the slot swap is instant, while the kill-escalation a
        # retirement runs can take ~10s (SIGTERM -> join(5) -> SIGKILL ->
        # join(5)). Running that under the lock would block every other
        # acquisition and every release for the whole window. A replaced slot is
        # unreachable from the pool the instant it is swapped out, so deferring
        # only its teardown is safe; the list is drained in the finally below,
        # before this returns.
        dead_slots: list[DocWorker] = []
        try:
            with self._lock:
                if self._slots is None:
                    self._slots = []
                # Reap retired slots before the scans. A retired slot has been
                # terminated and can never be handed out again (the handout scan
                # requires ``not retired``), so it must not keep occupying a pool
                # entry. The respawn scan below only replaces a slot whose process
                # is dead, which leaves a retired-but-still-``is_alive()`` slot
                # (SIGKILL could not reap it -- pathological) skipped by BOTH
                # scans and stranded in the pool forever: it held a PID while the
                # size budget grew around it, and ``len(self._slots)`` drifted
                # permanently above the usable count. Removing every retired slot
                # here keeps the pool and the budget consistent; the termination
                # and the process reap already ran when the slot was retired, so
                # nothing live is dropped. Re-running this on every loop iteration
                # also catches a slot retired by another thread while this
                # acquirer waited.
                self._slots = [each for each in self._slots if not each.retired]
                want = self._worker_count()
                # The budget starts once the lock is actually held. A caller
                # queued behind a slow in-lock operation would otherwise pay that
                # wait out of a budget it never consumed, and could give up on a
                # pool that had a free slot the whole time it was blocked.
                # A healthy pool releases a worker when its own task hits the
                # document deadline, so a full round of the pool is already
                # generous; this bound exists for the unhealthy case, where a busy
                # flag is never cleared and an unbounded wait would take the whole
                # pool down with it.
                deadline = time.monotonic() + timeout
                while True:
                    for worker in self._slots:
                        if not worker.busy and not worker.retired and worker.proc.is_alive():
                            worker.busy = True
                            return worker
                    for index, worker in enumerate(self._slots):
                        if not worker.busy and not worker.proc.is_alive():
                            fresh = self._spawn()
                            fresh.busy = True
                            self._slots[index] = fresh
                            dead_slots.append(worker)
                            return fresh
                    # Retired slots were reaped above, so this is the live usable
                    # set; when it is short of the configured size the pool grows
                    # by one rather than degrading to "no worker" -- the failure
                    # mode the reaped zombie slot used to cause (a slot nothing
                    # could recover shrinking the pool permanently).
                    usable = sum(1 for each in self._slots if not each.retired)
                    if usable < want:
                        fresh = self._spawn()
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
                    # Still a condition wait, not a poll loop: a release notifies
                    # and wakes immediately. The bounded re-check exists only so a
                    # stuck busy flag (nothing will ever notify) expires on its
                    # own.
                    self._lock.wait(timeout=min(remaining, self._recheck_seconds))
        finally:
            # Teardown of any slot swapped out above, now that the lock is free.
            # ``retire`` marks retired, terminates and reaps; this path keeps the
            # same retirement code as the timeout/child-death call sites.
            for worker in dead_slots:
                self.retire(worker)

    def retire(self, worker: DocWorker) -> None:
        """Terminate and reap one worker, leaving its (dead) slot for respawn.

        Respawn is lazy, on the next acquisition: a dead-idle slot costs nothing
        until the next document arrives, and lazy is what keeps a timeout or a
        child death at zero live children immediately.

        The retired mark is set before anything slow because retirement is not
        always under the lock: without it, the window between "terminated" and
        "respawned" would let an acquisition hand a doomed worker to the next
        caller.
        """
        worker.retired = True
        terminate_doc_process(worker.proc)
        with suppress(Exception):
            worker.conn.close()

    def release(self, worker: DocWorker) -> None:
        """Hand a slot back and wake everyone waiting for it."""
        with self._lock:
            # The worker may have been retired mid-task (timeout/child death).
            # Clearing busy is still correct: ``retired``, not ``busy``, is what
            # keeps a terminated worker out of circulation, and the notify lets
            # waiters reach the slot's lazy respawn instead of sitting on it.
            worker.busy = False
            self._lock.notify_all()

    def pids(self) -> list[int]:
        """Live worker PIDs: proves persistence, not spawn-per-task."""
        with self._lock:
            return [worker.proc.pid for worker in self._slots or [] if worker.proc.pid]

    def health(self) -> list[tuple[bool, bool, bool]]:
        """Per-worker ``(alive, reaped, busy)`` snapshot.

        Idle workers legitimately stay alive across calls, so the recoverable-
        failure contract is per worker, not process-global: a worker that died must
        be reaped (exitcode set, not a zombie) and none may be left busy mid-task.
        """
        with self._lock:
            return [
                (worker.proc.is_alive(), worker.proc.exitcode is not None, worker.busy)
                for worker in self._slots or []
            ]

    def teardown(self) -> None:
        """Terminate every pooled worker and forget the slots."""
        with self._lock:
            pool, self._slots = self._slots, None
            for worker in pool or []:
                self.retire(worker)

    def __len__(self) -> int:
        """How many slots the pool holds (none before the first acquisition)."""
        with self._lock:
            return len(self._slots or [])
