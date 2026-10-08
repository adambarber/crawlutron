"""curl_cffi impersonation and streaming: shared session, handle release, timeout and byte caps."""

import asyncio
import threading
from typing import Literal

import curl_cffi.requests as cffi_requests
import pytest
from curl_cffi.requests.exceptions import RequestException
from curl_cffi.requests.models import STREAM_END

import app.transport.fetchers as fetchers_mod
import app.transport.http_client_pool as http_mod
from app.transport.fetchers import impersonating_fetcher
from app.vocabulary.strategies import HTTP_CFFI_IMPERSONATE
from tests._fetch_outcome import FetchFailed
from tests._fetcher_factories import scripted_fetcher, strategy_request
from tests._policy_factories import canonical, make_base_policy
from tests._timing_harnesses import await_until
from tests._transport_doubles import real_cffi_fetch_outcome
from tests.helpers.server import SilentHandler, spawn_threaded_server

# The impersonating transport, bound once for this module. It used to be a local
# wrapper function called from nine places, each re-deciding that "the cffi
# fetcher" meant the impersonating binding -- the same value spelled nine times.
_DEFAULT_CFFI_FETCHER = impersonating_fetcher()


class TestCffiImpersonateStrategy:
    async def test_http_cffi_imersonate_result(self):
        from app.transport.cache import HttpCache
        from app.transport.http import http_strategy_result

        # A real HttpFetchResult, not a local class that has the same five
        # attributes: the strategy reads a production model, so a stand-in with
        # a matching shape can pass while the model drifts.
        cffi_fetcher = scripted_fetcher(
            status=200, ctype="text/html", body=b"<html><body>content</body></html>"
        )
        policy = make_base_policy(strategies=[HTTP_CFFI_IMPERSONATE]).model_copy(
            update={"extraction": "full"}
        )

        result = await http_strategy_result(
            request=strategy_request("http://x/", policy, HTTP_CFFI_IMPERSONATE),
            http_fetcher=cffi_fetcher,
            cache=HttpCache(),
        )
        assert result.classification == "rendered"


class TestCffiFetcherLiveFetch:
    async def test_http_cffi_import_fallback(self):
        # Live impersonated fetch. curl_cffi's socket/timer watchdog needs the
        # event loop to make real progress, which is why this used to need real
        # sleeps -- the suite's zero-second patch used to be a no-op that never
        # yielded at all. It yields now, so the transfer progresses.
        from tests.helpers.server import spawn_server

        server = spawn_server()
        try:
            result = await _DEFAULT_CFFI_FETCHER(canonical(server.base_url + "/"), {}, 10)
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


def _patch_session(stub_builder, response: _Response) -> _Session:
    session = _Session(response)
    stub_builder("cffi", lambda settings: session)
    return session


@pytest.fixture(autouse=True)
def _clean_pooled_client_queue():
    """Start each test with the pooled state released and the close queue empty.

    Pooled-client bookkeeping is irrelevant here and other suites leave entries
    behind; start clean so a drain here cannot try to close one of their fakes.
    ``reset_process_state`` is the one operation for this: it releases both
    pooled clients and clears the queue as a unit, which is the unit production
    releases them as, and these tests used to clear the queue by hand while
    separately nulling the curl_cffi session global -- two halves of a state no
    caller can reach.
    """
    http_mod.reset_process_state()


def _scheduled_closes(monkeypatch):
    """Capture ``_release_stream_noblock`` calls instead of the real scheduling."""
    released: list[object] = []
    monkeypatch.setattr(fetchers_mod, "_release_stream_noblock", lambda response: released.append(response))
    return released


def _scripted_stream(
    stub_builder, monkeypatch, items, *, headers=None
) -> tuple[_Response, _Session, list[object]]:
    """A cffi session whose queue replays `items`; return it and the release log.

    Every test here is about what one streaming fetch did with its response
    handle, so the arrangement is the same each time: one scripted queue, one
    session, one log of the releases the fetcher scheduled. ``items`` is the
    only thing that varies -- an exception part-way through, a body past the
    cap, a stream that ends -- and a queue that runs dry wedges rather than
    ending, so a script that does not say what happens next cannot pass
    silently.
    """
    response = _Response(items, headers=headers)
    return response, _patch_session(stub_builder, response), _scheduled_closes(monkeypatch)


async def test_request_exception_from_the_queue_releases_the_handle(
    stub_builder, monkeypatch, counting_server
):
    """The error exit: a queued RequestException must not orphan the handle."""
    url = f"http://127.0.0.1:{counting_server.server_port}/"
    response, _session, released = _scripted_stream(
        stub_builder, monkeypatch, [b"partial", RequestException("boom")]
    )

    result = await _DEFAULT_CFFI_FETCHER(canonical(url), {}, 5, 4096)

    assert result.status_code == 200, "the documented httpx fallback must still serve"
    # Served by the local server, so this is the fallback and not the scripted cffi
    # session: the assertion is about the handle, and it needs the fetch to finish.
    assert counting_server.hits == 1, "the fallback did not serve the page"
    assert released == [response], "the error path left the streamed response handle unreleased"
    assert response.awaited is False, "abandoning must not await a live producer"


async def test_cancellation_of_the_queue_wait_releases_the_handle(stub_builder, monkeypatch):
    """The cancel exit: the engine's per-URL budget cancels fetches routinely.

    The wait below is on the named state -- the fetch has taken the session
    and the producer is wedged in the queue -- not on a deadline of seconds.
    It used to need real sleeps only because the poll's ``asyncio.sleep`` was a
    no-op that never yielded, so the fetch task could never be scheduled at
    all; the zero-second patch yields now, so the poll works without wall time.
    """
    response, session, released = _scripted_stream(
        stub_builder, monkeypatch, [b"first"]
    )  # then the producer wedges

    fetch = asyncio.create_task(_DEFAULT_CFFI_FETCHER(canonical(URL), {}, 5, 4096))
    await await_until(
        lambda: bool(session.gets) and response.queue.drained,
        "the cffi fetch never reached the streaming queue wait",
    )
    fetch.cancel()
    with pytest.raises(asyncio.CancelledError):
        await fetch

    assert released == [response], "a cancelled cffi fetch abandoned its response handle on a shared session"
    assert response.awaited is False


async def test_cap_exceeded_and_still_releases_exactly_once(stub_builder, monkeypatch):
    """Boundary: the pre-existing abandon paths must not double-release.

    Wrapping the loop in a release-on-any-exit guard is the obvious shape for
    this fix; if it also fires on the paths that already release by hand, the
    producer gets two scheduled closes. One release per fetch, whichever exit
    was taken.
    """
    response, _session, released = _scripted_stream(stub_builder, monkeypatch, [b"x" * 100] * 5)

    result = await _DEFAULT_CFFI_FETCHER(canonical(URL), {}, 5, 50)

    assert result.status_code == 413
    assert len(released) == 1, f"expected one release, got {len(released)}"


async def test_clean_stream_end_awaits_the_close_and_never_abandons(stub_builder, monkeypatch):
    """Inverse case: a finished producer is safe to await, and required now.

    The shared session only recovers the handle for the next streamed request if
    the completed fetch closed it, so the success path must NOT downgrade to the
    non-blocking release.
    """
    response, _session, released = _scripted_stream(
        stub_builder, monkeypatch, [b"<html>ok</html>", STREAM_END]
    )

    result = await _DEFAULT_CFFI_FETCHER(canonical(URL), {}, 5, 4096)

    assert result.status_code == 200
    assert released == [], "a completed stream must be closed, not abandoned"
    assert response.awaited is True, "the success path must await the handle close"


class _StalledBodyHandler(SilentHandler):
    """200 + headers, then not one body byte until ``_STALL_RELEASE`` is set.

    Subclasses ``SilentHandler`` because the response methods used to be
    duck-typed onto a bare class. ``socketserver`` constructs a handler with
    ``(request, client_address, server)``, so the bare class raised
    ``TypeError: _StalledBodyHandler() takes no arguments`` on every request and
    the server reset the connection instead of stalling. The test below reads
    any transport error as a pass, so it stayed green having never run the path
    it names. ``counter.hits`` is what now proves the handler was reached.

    The stall is an ``Event`` rather than a sleep because the suite patches
    ``time.sleep`` to a no-op: a handler that waits by sleeping returns at once,
    the response ends, and the fetch fails for a reason that has nothing to do
    with the timeout under test. ``_STALL_RELEASE`` is set by the test's
    teardown, so the wedged handler thread does not outlive it.
    """

    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self.counter.hits += 1
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        self.wfile.flush()
        _STALL_RELEASE.wait()


_STALL_RELEASE = threading.Event()


@pytest.mark.real_cffi
def test_cffi_stream_timeout_honored_on_stalled_body(monkeypatch) -> None:
    """real_cffi: drives the genuine fetcher inside an isolated interpreter.

    The server answers 200 and its headers and then never sends a body byte,
    so the only two acceptable outcomes are a bounded failure or a rejection of
    the body. The harness owns the isolation and the join bound; what is
    asserted here is that neither of those two became a success.

    The target is loopback, so the child interpreter is allowed to reach one
    here rather than relying on the suite-wide default, which a strict-SSRF run
    does not set. The child inherits this process's environment.
    """
    monkeypatch.setenv("CRAWLUTRON_ALLOW_PRIVATE_TARGETS", "1")
    _STALL_RELEASE.clear()
    server = spawn_threaded_server(_StalledBodyHandler)
    try:
        outcome = real_cffi_fetch_outcome(
            f"http://127.0.0.1:{server.server_port}/",
            timeout_seconds=1,
            join_timeout=8.0,
        )
    finally:
        _STALL_RELEASE.set()
        server.shutdown()
        server.server_close()

    if isinstance(outcome, FetchFailed):
        # Any transport-error class is contract-fine (ReadError, Timeout,
        # ConnectionError...): the point is a bounded failure, not a hang.
        assert outcome.error, outcome
    else:
        # cffi returned; it must NOT be a 200-with-body (the stalled body
        # must surface as 413/timeout mapping, not a successful fetch).
        assert outcome.status_code != 200, "stalled body reported as a 200 success"

    # The load-bearing assertion. Both arms above are satisfiable by a server
    # that died before serving, which is exactly what this handler used to do:
    # every request reset, the reset surfaced as the accepted error class, and
    # the test reported the timeout honored without a body ever being withheld.
    # At least one, not exactly one: the impersonated fetcher retries a read
    # timeout, so the withheld body is requested more than once.
    assert server.hits >= 1, "the stall handler was never reached, so nothing was withheld"


class _FeedOnDemandQueue:
    """Feeds one chunk per get(), mirroring curl's bounded-queue backpressure:
    the producer only advances when the consumer reads, so counters measure
    consumption (the property under test), not producer progress."""

    def __init__(self, body: bytes, chunk_size: int):
        self._body = body
        self._chunk_size = chunk_size
        self._offset = 0

    async def get(self):
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
        self.headers: dict[str, str] = {}  # no content-length: chunked-style body
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
    #: The most recent client built, or ``None`` when none has been. Annotated
    #: because a class attribute initialized to ``None`` is otherwise typed as
    #: exactly that -- and the very next statement assigns a client to it.
    last_instance: "_FakeCffiClient | None" = None

    def __init__(self, body: bytes, **kwargs):
        self._body = body
        #: Set by ``get``; ``None`` until this client has answered a request.
        self.response: _FakeCffiResponse | None = None
        # Session acquisition is now construction (shared session),
        # not per-request __aenter__; record here so construction counts.
        type(self).last_instance = self

    async def __aenter__(self):
        type(self).last_instance = self
        return self

    async def __aexit__(self, *args) -> Literal[False]:
        return False

    async def get(self, url, **kwargs):
        self.response = _FakeCffiResponse(self._body)
        return self.response


def _patch_client(monkeypatch, body: bytes):
    _FakeCffiClient.last_instance = None
    monkeypatch.setattr(cffi_requests, "AsyncSession", lambda **kw: _FakeCffiClient(body))


async def _capped_fetch(monkeypatch, body_size: int):
    """One impersonated cffi fetch of ``body_size`` bytes against a 64-byte cap.

    The cap tests are about the streaming read stopping, not about the request,
    so the body, the cap and the URL are fixed here: spelled per test, a change
    to the cap was four edits and one of them was in a test that was asserting
    something else.
    """
    _patch_client(monkeypatch, b"x" * body_size)
    return await _DEFAULT_CFFI_FETCHER(canonical("http://example.test/page"), {}, 10, 64)


class TestCffiStreamingCap:
    async def test_chunked_body_over_cap_returns_413(self, monkeypatch):
        result = await _capped_fetch(monkeypatch, 10_000)
        assert result.status_code == 413

    async def test_real_client_class_exists(self):
        # Detection-gap guard: the cffi path once referenced AsyncClient,
        # which does not exist in curl_cffi>=0.7, so every impersonated
        # fetch silently fell back to plain httpx. Pin the real API.

        assert hasattr(cffi_requests, "AsyncSession")

    async def test_streaming_stops_shortly_after_cap(self, monkeypatch):
        result = await _capped_fetch(monkeypatch, 100_000)
        assert result.status_code == 413
        client = _FakeCffiClient.last_instance
        assert client is not None, "the capped fetch must have built a cffi client"
        assert client.response is not None, "the capped fetch must have got a response to stream"
        consumed = client.response.chunks_yielded * 1024
        assert consumed <= 64 + 1024


async def _fetch_twice(base_url):

    async def one():
        return await _DEFAULT_CFFI_FETCHER(canonical(base_url + "/"), {}, 10, 1024 * 1024)

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
    from app.transport import http_client_pool as http_mod

    constructions = 0

    # Subclassing ``cffi_requests.AsyncSession`` directly rather than a variable
    # bound to it: the base is resolved when this statement runs, and the test
    # patches the module attribute afterwards, so the counter still wraps the
    # real session.
    class _CountingSession(cffi_requests.AsyncSession):
        def __init__(self, *args, **kwargs):
            nonlocal constructions
            constructions += 1
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(cffi_requests, "AsyncSession", _CountingSession)
    try:
        r1, r2 = await _fetch_twice(base_url)
    finally:
        await http_mod.close_cffi_session()
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert constructions == 1


def test_cffi_session_rebuilt_on_loop_change(monkeypatch):
    """A new event loop gets a fresh session (no sockets bound to a dead loop)."""
    from app.transport import http_client_pool as http_mod

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


async def test_cffi_missing_falls_back_to_httpx(monkeypatch, counting_server):
    """Without curl_cffi the fetcher still delegates to the plain httpx path.

    Answered by a real local server rather than a fake transport, and asserted
    through ``server.hits``: the point is that the page is fetched by *something*
    when the impersonating transport is absent, and a stubbed fallback would
    only prove the stub was called.
    """
    import sys

    from app.transport import http_client_pool as http_mod

    monkeypatch.setitem(sys.modules, "curl_cffi.requests", None)
    url = f"http://127.0.0.1:{counting_server.server_port}/"
    try:
        resp = await _DEFAULT_CFFI_FETCHER(canonical(url), {}, 5, 1024)
    finally:
        await http_mod.close_cffi_session()
    assert resp.status_code == 200
    assert counting_server.hits == 1, "the fallback did not serve the page"
