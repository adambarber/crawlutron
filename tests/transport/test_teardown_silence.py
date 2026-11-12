"""The teardown-silence class (#678): a failed best-effort teardown leaves evidence. Four sites suppress
close/harvest failures; each now records the condition in the #643 decline record -- structured fields,
bounded by key space, never log parsing. The success path records NOTHING (bounded logging means bounded).
"""

import asyncio
import logging
from typing import cast

from crawlutron.models.normalized_url import NormalizedUrl
from crawlutron.runtime.resource_lifecycle import (
    clear_declined_closes,
    declined_closes,
)
from crawlutron.transport.browser_cookies import harvest_session_cookies
from crawlutron.transport.browser_render_phases import ReleasePlan, release_page
from crawlutron.transport.cffi_transport import release_stream_noblock
from crawlutron.transport.pooled_clients import safe_aclose
from crawlutron.transport.render_page import RenderPage
from crawlutron.transport.transport import Closeable
from tests._policy_factories import canonical


class _FailingClose:
    """A pooled client whose aclose raises (both protocol handles present)."""

    async def aclose(self) -> None:
        raise RuntimeError("close failed")

    async def close(self) -> None:
        raise RuntimeError("close failed")


class _FailingStream:
    """A stream double whose aclose raises inside its task."""

    def __init__(self) -> None:
        self.calls = 0

    async def aclose(self) -> None:
        self.calls += 1
        raise RuntimeError("stream close failed")


class _CancelledStream:
    """A stream double whose aclose fails with cancellation."""

    async def aclose(self) -> object:
        raise asyncio.CancelledError()


def _decline(reason: str) -> bool:
    return any(key[0] == reason for key in declined_closes())


async def _drain(tasks: int = 50):
    for _ in range(tasks):
        await asyncio.sleep(0)


async def test_a_failed_client_close_records_the_decline():
    """S4: the suppressed close error is recorded, not lost."""
    clear_declined_closes()
    await safe_aclose(_FailingClose())
    assert _decline("aclose_failed"), f"the close failure was not recorded: {declined_closes()!r}"


async def test_a_successful_client_close_records_nothing():
    """Negative: the success path records nothing (bounded means bounded)."""

    class _Good:
        async def aclose(self) -> object:
            pass

        async def close(self) -> object:
            pass

    clear_declined_closes()
    await safe_aclose(_Good())
    assert declined_closes() == {}, f"a successful close was recorded: {declined_closes()!r}"


async def test_a_failed_stream_close_records_the_decline():
    """N2: the retrieved-then-dropped task exception is recorded."""
    clear_declined_closes()
    stream = _FailingStream()
    release_stream_noblock(stream)
    await _drain()
    assert _decline("stream_close_failed"), (
        f"the stream close failure was not recorded: {declined_closes()!r}"
    )


async def test_a_successful_stream_close_records_nothing():
    clear_declined_closes()

    class _Stream:
        async def aclose(self) -> None:
            pass

    release_stream_noblock(_Stream())
    await _drain()
    assert declined_closes() == {}, f"a successful close was recorded: {declined_closes()!r}"


async def test_a_cancelled_stream_close_records_nothing():
    """N2: a cancelled close is not evidence -- the task.cancelled() guard."""
    clear_declined_closes()
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    captured: list[object] = []

    def capture(_loop: object, context: object) -> None:
        captured.append(context)

    loop.set_exception_handler(capture)
    try:
        stream = _CancelledStream()
        release_stream_noblock(cast("Closeable", stream))
        await _drain()
    finally:
        loop.set_exception_handler(previous_handler)
    assert not captured, f"a cancelled close let task.exception() escape into the loop: {captured!r}"
    assert declined_closes() == {}, f"a cancelled close was recorded: {declined_closes()!r}"


async def test_an_uncancelled_failing_stream_close_records_exactly_one_entry():
    """N2: a failing close that was not cancelled records exactly one entry."""
    clear_declined_closes()
    stream = _FailingStream()
    release_stream_noblock(cast("Closeable", stream))
    await _drain()
    record = declined_closes()
    assert len(record) == 1, f"exactly one decline expected: {record!r}"
    assert ("stream_close_failed", "_FailingStream") in record, (
        f"the entry is not the stream close decline: {record!r}"
    )


class _RouteContext:
    """Route-install surface the guard wiring touches before the harvest."""

    async def route(self, pattern, handler):
        pass

    def route_web_socket(self, pattern, handler):
        pass


class _WedgedContext(_RouteContext):
    """cookies() never resolves -- the harvest timeout shape."""

    async def cookies(self, url):
        await asyncio.Event().wait()


class _BadContext(_RouteContext):
    """cookies() raises -- the harvest failure shape."""

    async def cookies(self, url):
        raise RuntimeError("context gone")


class _GoodContext(_RouteContext):
    """cookies() answers with one session cookie."""

    async def cookies(self, url):
        return [{"name": "sess", "value": "abc"}]


class _Page:
    """A minimal render page double carrying only the harvest context."""

    def __init__(self, context) -> None:
        self.context = context


async def _release_with(page: RenderPage, url: NormalizedUrl, closed: list[object]) -> None:
    async def close_page(p):
        closed.append(p)

    def note_render():
        pass

    plan = ReleasePlan(page=page, url=url, profile={})
    await release_page(plan, harvest_timeout=0.01, close_page=close_page, note_render=note_render)


async def test_a_harvest_timeout_records_the_decline():
    """N3: a silently lost harvest timeout is recorded; the close still runs."""
    clear_declined_closes()
    url = canonical("http://harvest-timeout.test/page")
    closed: list[object] = []
    await _release_with(cast(RenderPage, _Page(_WedgedContext())), url, closed)
    assert _decline("harvest_timeout"), f"the harvest timeout was not recorded: {declined_closes()!r}"
    assert closed, "the page close must still happen after a harvest timeout"


async def test_a_harvest_failure_records_the_decline():
    clear_declined_closes()
    url = canonical("http://harvest-bad.test/page")
    closed: list[object] = []
    await _release_with(cast(RenderPage, _Page(_BadContext())), url, closed)
    assert _decline("harvest_failed"), f"the harvest failure was not recorded: {declined_closes()!r}"
    assert closed, "the page close must still happen after a harvest failure"


async def test_a_successful_harvest_records_nothing(caplog):
    clear_declined_closes()
    url = canonical("http://harvest-good.test/page")
    closed: list[object] = []
    with caplog.at_level(logging.DEBUG):
        await _release_with(cast(RenderPage, _Page(_GoodContext())), url, closed)
    assert declined_closes() == {}, f"a successful harvest was recorded: {declined_closes()!r}"
    assert not [r for r in caplog.records if "harvest_failed" in r.getMessage()]


async def test_harvest_failure_logs_at_warning_not_debug(caplog):
    """N4: severity was the whole finding -- DEBUG is invisible in production."""
    clear_declined_closes()
    url = canonical("http://harvest-bad-log.test/page")
    with caplog.at_level(logging.INFO):
        await harvest_session_cookies(cast(RenderPage, _Page(_BadContext())), url)
    failures = [r for r in caplog.records if r.__dict__.get("error") == "context gone"]
    assert failures, f"the harvest failure left no log record: {caplog.records!r}"
    assert failures[0].levelno >= logging.WARNING, (
        f"the harvest failure logged at {logging.getLevelName(failures[0].levelno)}, not WARNING"
    )
