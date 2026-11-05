"""Retirement kills a native conversion mid-flight; an idle worker exits clean."""

from __future__ import annotations

import os

import crawlutron.extraction.doc_pool as dp
from crawlutron.extraction.doc_pool import DocExtractionPool, spawn_doc_worker


class _EofPipe:
    """A pipe whose first recv raises EOFError: the clean pipe-close exit."""

    def send(self, item):
        pass

    def recv(self):
        raise EOFError

    def close(self):
        pass


def test_clean_exit_skips_native_teardown(monkeypatch):
    """A worker that sees pipe EOF must exit through os._exit(0), never return
    into interpreter shutdown.

    #510: onnxruntime's telemetry WorkerThread aborts the process during native
    module finalization ('libc++abi: recursive_mutex lock failed'). The abort is
    not suppressible from our side -- the only seal is that the worker never
    runs native teardown at all. os._exit(0) on the clean path removes the
    faulty code path (masking would be swallowing the abort line instead)."""
    exits: list[int] = []
    monkeypatch.setattr(os, "_exit", lambda code=0: exits.append(code))
    monkeypatch.setattr(dp, "configure_doc_worker_runtime", lambda: None)
    monkeypatch.setattr(dp, "configure_doc_worker_logging", lambda: None)

    dp.doc_worker_main(_EofPipe())

    assert exits == [0], (
        "the clean pipe-close exit must terminate the worker via os._exit(0) -- "
        "returning into interpreter shutdown races onnxruntime's telemetry "
        "thread into the libc++ recursive-mutex abort (#510)"
    )


def test_doc_worker_runtime_disables_onnxruntime_telemetry(monkeypatch):
    """The worker entry must disable onnxruntime telemetry events.

    onnxruntime's telemetry thread is the one that aborts with the libc++
    recursive-mutex noise when a worker is torn down mid-flight, and it makes a
    network call a worker has no business making."""
    import crawlutron.extraction.doc_pool as dp

    calls: list[str] = []
    monkeypatch.setattr(dp, "configure_doc_worker_logging", lambda: calls.append("logging"))
    import sys

    fake = type(sys)("fake_onnxruntime")
    # The stub module stands in for onnxruntime's own surface: the dynamic
    # module write goes through object.__setattr__, the doubles' seam spelling.
    object.__setattr__(fake, "disable_telemetry_events", lambda: calls.append("telemetry"))
    monkeypatch.setitem(sys.modules, "onnxruntime", fake)

    dp.configure_doc_worker_runtime()
    assert "telemetry" in calls, (
        "configure_doc_worker_runtime must disable onnxruntime telemetry events; "
        "the telemetry thread is the libc++ recursive-mutex abort source and a "
        "worker has no business making a network call"
    )


def test_doc_worker_runtime_survives_a_missing_onnxruntime():
    """The quieting is best-effort: a worker without onnxruntime still starts."""
    import sys

    import crawlutron.extraction.doc_pool as dp

    saved = sys.modules.pop("onnxruntime", None)
    try:
        dp.configure_doc_worker_runtime()  # must not raise
    finally:
        if saved is not None:
            sys.modules["onnxruntime"] = saved


def _pool() -> DocExtractionPool:
    return DocExtractionPool(worker_count=lambda: 1, enabled=lambda: True)


def test_idle_worker_retires_with_a_clean_exit():
    """An idle worker sits at ``recv()``: pipe-close ends its loop and it exits 0.

    SIGTERM at that point is what races onnxruntime's telemetry thread into the
    libc++ recursive-mutex abort that pollutes post-suite output."""
    worker = spawn_doc_worker(ready_timeout=60.0)
    _pool().retire(worker)
    assert worker.proc.exitcode == 0, (
        f"idle worker exited {worker.proc.exitcode}; retirement must close the "
        "pipe first and only escalate to a signal if the worker misses the grace"
    )


def test_retire_returns_promptly_for_an_already_dead_worker():
    """A reaped slot must not pay the retirement grace a second time."""
    worker = spawn_doc_worker(ready_timeout=60.0)
    worker.proc.kill()
    worker.proc.join(timeout=10)
    import time

    started = time.monotonic()
    _pool().retire(worker)
    assert time.monotonic() - started < 1.0
    assert worker.proc.exitcode is not None
