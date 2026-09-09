"""H1 regression: the streaming cffi fetcher must honor its timeout contract.

Pre-fix, `stream=True` + `aiter_content()` wedged forever on a stalled
response: curl_cffi 0.16 replaces TIMEOUT_MS with a LOW_SPEED abort in
stream mode, and `aiter_content`'s exception path awaits `aclose()`, which
blocks on the never-finishing stream task before re-raising the very
timeout it received.

The fetch runs in a SUBPROCESS on purpose: under the suite's fast_sleeps
patch, curl_cffi's internal watchdog busy-spins the GIL and freezes the
whole pytest process (only SIGKILL recovers it). A subprocess gives a clean
interpreter, immune to conftest mocks and loop plugins, and a hard
wall-clock the test can enforce.
"""

import multiprocessing
import os
import time

import pytest
from helpers.server import spawn_threaded_server


class _StalledBodyHandler:
    """200 + headers, then never a body byte."""

    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802 - http.server API
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        self.wfile.flush()
        time.sleep(3600)

    def log_message(self, *args):  # noqa: ANN002
        pass


def _fetch_worker(url: str, timeout: float, out_queue: multiprocessing.Queue) -> None:
    import asyncio

    os.environ["BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS"] = "1"

    from app.crawl.strategies.http import _default_cffi_fetcher

    async def _run():
        return await _default_cffi_fetcher(url, {}, int(timeout), max_content_length=10 * 1024 * 1024)

    try:
        result = asyncio.run(_run())
        out_queue.put(("ok", result.status_code, len(result.body)))
    except Exception as error:
        out_queue.put(("err", type(error).__name__, str(error)[:120]))


@pytest.mark.real_cffi
def test_cffi_stream_timeout_honored_on_stalled_body() -> None:
    """real_cffi: drives the genuine fetcher inside an isolated interpreter."""
    server = spawn_threaded_server(_StalledBodyHandler)
    url = f"http://127.0.0.1:{server.server_port}/"
    out_queue: multiprocessing.Queue = multiprocessing.Queue()
    worker = multiprocessing.Process(target=_fetch_worker, args=(url, 1, out_queue))
    started = time.monotonic()
    try:
        worker.start()
        worker.join(timeout=8.0)
        if worker.is_alive():
            worker.terminate()
            worker.join()
            pytest.fail("cffi fetcher hung past 8s on a stalled stream (timeout contract broken)")
    finally:
        server.shutdown()
        server.server_close()

    assert worker.exitcode == 0, f"worker crashed: exitcode={worker.exitcode}"
    assert not out_queue.empty(), "worker produced no result"
    outcome, status_or_exc, _detail = out_queue.get()
    elapsed = time.monotonic() - started
    assert elapsed < 8.0, f"fetch took {elapsed:.1f}s; timeout contract broken"
    if outcome == "ok":
        # cffi returned; it must NOT be a 200-with-body (the stalled body
        # must surface as 413/timeout mapping, not a successful fetch).
        assert status_or_exc != 200, "stalled body reported as a 200 success"
    else:
        # Any transport-error class is contract-fine (ReadError, Timeout,
        # ConnectionError...): the point is a bounded failure, not a hang.
        assert status_or_exc
