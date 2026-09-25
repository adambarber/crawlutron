"""curl_cffi impersonation and streaming: shared session, handle release, timeout and byte caps."""

import asyncio
import multiprocessing
import os
import time

import pytest
from _timing_harnesses import await_until
from curl_cffi.requests.exceptions import RequestException
from curl_cffi.requests.models import STREAM_END
from helpers.server import spawn_threaded_server

import app.crawl.strategies.fetchers as fetchers_mod
import app.crawl.strategies.http_client_pool as http_mod
from app.crawl.strategies.fetchers import _default_cffi_fetcher as _default_cffi_fetcher_with_policy
from app.validation import HostPolicy


async def _default_cffi_fetcher(*args, policy=HostPolicy.unrestricted(), **kwargs):
    return await _default_cffi_fetcher_with_policy(*args, policy=policy, **kwargs)


class TestCffiImpersonateStrategy:
    async def test_http_cffi_imersonate_result(self):
        from app.crawl.cache import HttpCache
        from app.crawl.strategies.http import _http_strategy_result

        def cffi_fetcher(url, headers, timeout, max_content_length=50 * 1024 * 1024):
            class Resp:
                status_code = 200
                headers = {"content-type": "text/html"}
                body = b"<html><body>content</body></html>"
                final_url = url
                elapsed_ms = 10

            return Resp()

        result = await _http_strategy_result(
            original_url="http://x/",
            normalized_url="http://x/",
            strategy="HTTP_CFFI_IMPERSONATE",
            policy={
                "strategies": ["HTTP_CFFI_IMPERSONATE"],
                "extraction": "full",
                "headers": {},
                "limits": {"timeout_seconds": 5, "max_retries": 0, "max_discovered_links": 10},
            },
            http_fetcher=cffi_fetcher,
            cache=HttpCache(),
            host_policy=HostPolicy.unrestricted(),
        )
        assert result.classification == "rendered"


class TestCffiFetcherLiveFetch:
    async def test_http_cffi_import_fallback(self):
        # Live impersonated fetch. curl_cffi's socket/timer watchdog needs the
        # event loop to make real progress, which is why this used to need real
        # sleeps -- the suite's zero-second patch used to be a no-op that never
        # yielded at all. It yields now, so the transfer progresses.
        from helpers.server import spawn_server

        server = spawn_server()
        try:
            result = await _default_cffi_fetcher(server.base_url + "/", {}, 10, 50 * 1024 * 1024)
            assert result.status_code == 200
        finally:
            server.stop()


URL = "http://example.test/page"


class _Queue:
    """Yields scripted items then wedges, like a producer that never finishes.

    The wedge is an ``Event`` that is never set rather than a long ``sleep``:
    the suite patches ``asyncio.sleep`` to be instant by default, and a stream
    that "ends immediately" would take the completion path instead of the
    abandon path this test needs. A never-set event cannot be short-circuited by
    any sleep patch, so the cancel is guaranteed to land inside the queue wait.
    """

    def __init__(self, items):
        self._items = list(items)
        self._wedge = asyncio.Event()
        self.drained = False

    async def get(self):
        if self._items:
            await asyncio.sleep(0)
            return self._items.pop(0)
        self.drained = True
        await self._wedge.wait()


class _Response:
    def __init__(self, items, *, headers=None):
        self.status_code = 200
        self.headers = headers if headers is not None else {}
        self.queue = _Queue(items)
        self.aclose_calls = 0
        self.awaited = False

    async def aclose(self):
        # Flips only if the caller actually waits for the close to run, which
        # is the behaviour the stall/cap paths deliberately avoid.
        self.awaited = True
        self.aclose_calls += 1


class _Session:
    def __init__(self, response):
        self._response = response
        self.gets = 0

    async def get(self, url, **kwargs):
        self.gets += 1
        return self._response


def _patch_session(monkeypatch, response: _Response) -> _Session:
    session = _Session(response)
    monkeypatch.setattr(http_mod, "_DEFAULT_CFFI_SESSION", session)
    monkeypatch.setattr(http_mod, "_build_cffi_session", lambda *a, **k: session)
    return session


async def _fall_back_to_httpx(
    url,
    headers,
    timeout_seconds,
    max_content_length=0,
    *,
    policy,
):
    return "httpx-fallback"


@pytest.fixture(autouse=True)
def _no_real_fallback(monkeypatch):
    monkeypatch.setattr(fetchers_mod, "_default_http_fetcher", _fall_back_to_httpx)
    # Pooled-client bookkeeping is irrelevant here and other suites leave
    # entries behind; start clean so the drain cannot close our fake.
    with http_mod._PENDING_CLIENT_CLOSE_LOCK:
        http_mod._PENDING_CLIENT_CLOSE.clear()


def _scheduled_closes(monkeypatch):
    """Capture ``_release_stream_noblock`` calls instead of the real scheduling."""
    released: list[object] = []
    monkeypatch.setattr(
        fetchers_mod, "_release_stream_noblock", lambda response: released.append(response)
    )
    return released


async def test_request_exception_from_the_queue_releases_the_handle(monkeypatch):
    """The error exit: a queued RequestException must not orphan the handle."""
    response = _Response([b"partial", RequestException("boom")])
    _patch_session(monkeypatch, response)
    released = _scheduled_closes(monkeypatch)

    result = await _default_cffi_fetcher(URL, {}, 5, 4096)

    assert result == "httpx-fallback", "the documented httpx fallback must still serve"
    assert released == [response], (
        "the error path left the streamed response handle unreleased"
    )
    assert response.awaited is False, "abandoning must not await a live producer"


async def test_cancellation_of_the_queue_wait_releases_the_handle(monkeypatch):
    """The cancel exit: the engine's per-URL budget cancels fetches routinely.

    The wait below is on the named state -- the fetch has taken the session
    and the producer is wedged in the queue -- not on a deadline of seconds.
    It used to need real sleeps only because the poll's ``asyncio.sleep`` was a
    no-op that never yielded, so the fetch task could never be scheduled at
    all; the zero-second patch yields now, so the poll works without wall time.
    """
    response = _Response([b"first"])  # then the producer wedges forever
    session = _patch_session(monkeypatch, response)
    released = _scheduled_closes(monkeypatch)

    fetch = asyncio.create_task(_default_cffi_fetcher(URL, {}, 5, 4096))
    await await_until(
        lambda: bool(session.gets) and response.queue.drained,
        "the cffi fetch never reached the streaming queue wait",
    )
    fetch.cancel()
    with pytest.raises(asyncio.CancelledError):
        await fetch

    assert released == [response], (
        "a cancelled cffi fetch abandoned its response handle on a shared session"
    )
    assert response.awaited is False


async def test_cap_exceeded_and_still_releases_exactly_once(monkeypatch):
    """Boundary: the pre-existing abandon paths must not double-release.

    Wrapping the loop in a release-on-any-exit guard is the obvious shape for
    this fix; if it also fires on the paths that already release by hand, the
    producer gets two scheduled closes. One release per fetch, whichever exit
    was taken.
    """
    response = _Response([b"x" * 100] * 5)
    _patch_session(monkeypatch, response)
    released = _scheduled_closes(monkeypatch)

    result = await _default_cffi_fetcher(URL, {}, 5, 50)

    assert result.status_code == 413
    assert len(released) == 1, f"expected one release, got {len(released)}"


async def test_clean_stream_end_awaits_the_close_and_never_abandons(monkeypatch):
    """Inverse case: a finished producer is safe to await, and required now.

    The shared session only recovers the handle for the next streamed request if
    the completed fetch closed it, so the success path must NOT downgrade to the
    non-blocking release.
    """
    response = _Response([b"<html>ok</html>", STREAM_END])
    _patch_session(monkeypatch, response)
    released = _scheduled_closes(monkeypatch)

    result = await _default_cffi_fetcher(URL, {}, 5, 4096)

    assert result.status_code == 200
    assert released == [], "a completed stream must be closed, not abandoned"
    assert response.awaited is True, "the success path must await the handle close"


class _StalledBodyHandler:
    """200 + headers, then never a body byte."""

    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        self.wfile.flush()
        time.sleep(3600)

    def log_message(self, *args):
        pass


def _fetch_worker(url: str, timeout: float, out_queue: multiprocessing.Queue) -> None:
    import asyncio

    os.environ["BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS"] = "1"

    from app.crawl.strategies.fetchers import _default_cffi_fetcher
    from app.validation import HostPolicy

    async def _run():
        return await _default_cffi_fetcher(
            url,
            {},
            int(timeout),
            max_content_length=10 * 1024 * 1024,
            policy=HostPolicy.unrestricted(),
        )

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


class _FeedOnDemandQueue:
    """Feeds one chunk per get(), mirroring curl's bounded-queue backpressure:
    the producer only advances when the consumer reads, so counters measure
    consumption (the property under test), not producer progress."""

    def __init__(self, body: bytes, chunk_size: int):
        self._body = body
        self._chunk_size = chunk_size
        self._offset = 0

    async def get(self):
        from curl_cffi.requests.models import STREAM_END

        if self._offset >= len(self._body):
            return STREAM_END
        chunk = self._body[self._offset : self._offset + self._chunk_size]
        self._offset += len(chunk)
        self.chunks_yielded = self._offset // self._chunk_size + (1 if self._offset % self._chunk_size else 0)
        return chunk


class _FakeCffiResponse:
    def __init__(self, body: bytes, *, chunk_size=1024):
        self.status_code = 200
        self.url = "http://example.test/page"
        self.headers = {}  # no content-length: chunked-style body
        self.elapsed = 0.01
        # The fetcher consumes queue items directly (the aiter_content
        # wrapper is bypassed because its aclose wedges on aborted streams).
        self.queue = _FeedOnDemandQueue(body, chunk_size)

    @property
    def content(self):
        # Emulates buffering: hands over the whole body at once.
        return b""

    @property
    def chunks_yielded(self):
        return self.queue.chunks_yielded


class _FakeCffiClient:
    last_instance = None

    def __init__(self, body: bytes, **kwargs):
        self._body = body
        self.response = None
        # Session acquisition is now construction (shared session, perf #4),
        # not per-request __aenter__; record here so construction counts.
        type(self).last_instance = self

    async def __aenter__(self):
        type(self).last_instance = self
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, **kwargs):
        self.response = _FakeCffiResponse(self._body)
        return self.response


def _patch_client(monkeypatch, body: bytes):
    import curl_cffi.requests as cffi_requests

    _FakeCffiClient.last_instance = None
    monkeypatch.setattr(cffi_requests, "AsyncSession", lambda **kw: _FakeCffiClient(body))


class TestCffiStreamingCap:
    async def test_chunked_body_over_cap_returns_413(self, monkeypatch):
        _patch_client(monkeypatch, b"x" * 10_000)
        result = await _default_cffi_fetcher("http://example.test/page", {}, 10, 64)
        assert result.status_code == 413

    async def test_real_client_class_exists(self):
        # Detection-gap guard: the cffi path once referenced AsyncClient,
        # which does not exist in curl_cffi>=0.7, so every impersonated
        # fetch silently fell back to plain httpx. Pin the real API.
        import curl_cffi.requests as cffi_requests

        assert hasattr(cffi_requests, "AsyncSession")

    async def test_streaming_stops_shortly_after_cap(self, monkeypatch):
        _patch_client(monkeypatch, b"x" * 100_000)
        result = await _default_cffi_fetcher("http://example.test/page", {}, 10, 64)
        assert result.status_code == 413
        consumed = _FakeCffiClient.last_instance.response.chunks_yielded * 1024
        assert consumed <= 64 + 1024


async def _fetch_twice(base_url):

    async def one():
        return await _default_cffi_fetcher(base_url + "/", {}, 10, 1024 * 1024)

    r1 = await one()
    r2 = await one()
    return r1, r2


async def test_cffi_session_reused_across_requests(base_url, monkeypatch):
    """Two sequential fetches must construct exactly one AsyncSession.

    curl_cffi's socket/timer watchdog needs the event loop to make real
    progress, which is why this used to need real sleeps -- the suite's
    zero-second patch used to be a no-op that never yielded at all. It yields
    now, so the transfers progress.
    """
    import curl_cffi.requests as cffi_mod

    from app.crawl.strategies import http_client_pool as http_mod

    real_session = cffi_mod.AsyncSession
    constructions = 0

    class _CountingSession(real_session):
        def __init__(self, *args, **kwargs):
            nonlocal constructions
            constructions += 1
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(cffi_mod, "AsyncSession", _CountingSession)
    monkeypatch.setattr(http_mod, "_DEFAULT_CFFI_SESSION", None)
    try:
        r1, r2 = await _fetch_twice(base_url)
    finally:
        await http_mod.close_cffi_session()
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert constructions == 1


def test_cffi_session_rebuilt_on_loop_change(monkeypatch):
    """A new event loop gets a fresh session (no sockets bound to a dead loop)."""
    from app.crawl.strategies import http_client_pool as http_mod

    async def _get():
        return http_mod.get_cffi_session()

    async def _close():
        await http_mod.close_cffi_session()

    s1 = asyncio.run(_get())
    assert s1 is not None
    s2 = asyncio.run(_get())
    try:
        assert s1 is not s2
    finally:
        asyncio.run(_close())


async def test_cffi_missing_falls_back_to_httpx(monkeypatch):
    """Without curl_cffi the fetcher still delegates to the plain httpx path."""
    import sys

    from app.crawl.strategies import http_client_pool as http_mod

    monkeypatch.setitem(sys.modules, "curl_cffi.requests", None)
    monkeypatch.setattr(http_mod, "_DEFAULT_CFFI_SESSION", None)
    seen: list[str] = []

    async def fake_http(url, headers, timeout_seconds, max_content_length=0, *, policy):
        seen.append(url)

        class Resp:
            status_code = 200
            headers = {"content-type": "text/html"}
            body = b"<html><body>fallback</body></html>"
            final_url = url
            elapsed_ms = 1

        return Resp()

    monkeypatch.setattr(fetchers_mod, "_default_http_fetcher", fake_http)
    try:
        resp = await _default_cffi_fetcher("http://example.com/", {}, 5, 1024)
    finally:
        await http_mod.close_cffi_session()
    assert resp.status_code == 200
    assert seen == ["http://example.com/"]
