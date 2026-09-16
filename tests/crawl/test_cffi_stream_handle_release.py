"""C1: every exit from the cffi streaming loop must release the response handle.

The impersonated fetcher reads curl's queue directly (the ``aiter_content``
wrapper wedges on aborted streams), and the shared ``AsyncSession`` is now
long-lived -- one per event loop, reused across every crawl -- so a handle left
busy is not reclaimed by a per-request ``async with`` anymore. The existing
paths that *do* release are the stall and the size-cap abandon, both through
``_release_stream_noblock`` because awaiting ``aclose()`` would join the very
producer being escaped.

Two exits were missed:

  * a ``RequestException`` arriving as a queue item, which the loop re-raises
    straight out of the ``while True`` body, and
  * cancellation of the ``await queue.get()`` -- the ordinary case, because the
    engine cancels in-flight fetches on its per-URL budget.

Both abandon the handle. These tests drive the loop through both exits and assert
the non-blocking release fires. ``aclose`` is asserted to be *scheduled, not
awaited*: a hostile producer must never be joined from the abandoning path.
"""

import asyncio
import time

import pytest
from curl_cffi.requests.exceptions import RequestException
from curl_cffi.requests.models import STREAM_END

import app.crawl.strategies.http as http_mod
from app.crawl.strategies.http import _default_cffi_fetcher

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


async def _fall_back_to_httpx(url, headers, timeout_seconds, max_content_length=0):
    return "httpx-fallback"


@pytest.fixture(autouse=True)
def _no_real_fallback(monkeypatch):
    monkeypatch.setattr(http_mod, "_default_http_fetcher", _fall_back_to_httpx)
    # Pooled-client bookkeeping is irrelevant here and other suites leave
    # entries behind; start clean so the drain cannot close our fake.
    with http_mod._PENDING_CLIENT_CLOSE_LOCK:
        http_mod._PENDING_CLIENT_CLOSE.clear()


def _scheduled_closes(monkeypatch):
    """Capture ``_release_stream_noblock`` calls instead of the real scheduling."""
    released: list[object] = []
    monkeypatch.setattr(
        http_mod, "_release_stream_noblock", lambda response: released.append(response)
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


async def test_cancellation_of_the_queue_wait_releases_the_handle(real_sleeps, monkeypatch):
    """The cancel exit: the engine's per-URL budget cancels fetches routinely.

    Needs real_sleeps: the wait below polls with ``asyncio.sleep``, which the
    autouse fast_sleeps patch makes a no-op, and a no-op sleep never yields to
    the event loop -- the fetch task would never be scheduled and this poll
    would spin until its deadline. Real sleeps are what let the two tasks
    interleave at all.
    """
    response = _Response([b"first"])  # then the producer wedges forever
    session = _patch_session(monkeypatch, response)
    released = _scheduled_closes(monkeypatch)

    fetch = asyncio.create_task(_default_cffi_fetcher(URL, {}, 5, 4096))
    deadline = time.monotonic() + 10.0
    while not (session.gets and response.queue.drained):
        if time.monotonic() > deadline:
            pytest.fail("cffi fetch never reached the streaming queue wait")
        await asyncio.sleep(0.01)
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
