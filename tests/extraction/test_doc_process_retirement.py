"""Retirement kills a native conversion mid-flight; an idle worker exits clean."""

from __future__ import annotations

from app.orchestration.doc_pool import DocExtractionPool, spawn_doc_worker


def test_doc_worker_runtime_disables_onnxruntime_telemetry(monkeypatch):
    """The worker entry must disable onnxruntime telemetry events.

    onnxruntime's telemetry thread is the one that aborts with the libc++
    recursive-mutex noise when a worker is torn down mid-flight, and it makes a
    network call a worker has no business making."""
    import app.orchestration.doc_pool as dp

    calls: list[str] = []
    monkeypatch.setattr(dp, "configure_doc_worker_logging", lambda: calls.append("logging"))
    import sys

    fake = type(sys)("fake_onnxruntime")
    fake.disable_telemetry_events = lambda: calls.append("telemetry")
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

    import app.orchestration.doc_pool as dp

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
