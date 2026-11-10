"""The fast_sleeps leak detector: a sibling monkeypatch of asyncio.sleep must
not re-install a stale fast sleep over the suite's sleep for later tests.
"""

import asyncio
import time

import pytest
from starlette.types import Message

import tests.conftest as cf
from crawlutron.api.middleware import limit_request_body
from tests._asgi_request import make_request, refuse_never


def test_body_monkeypatches_sleep(monkeypatch):
    async def fake_sleep(delay, *args, **kwargs):
        await asyncio.sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)


@pytest.mark.real_sleeps
def test_sleep_is_still_real():
    # The bound sleep is the stable dispatcher (never a sibling's captured
    # restore), and the fast mode is OFF for a real_sleeps-marked test.
    assert asyncio.sleep is cf._dispatched_asyncio_sleep, asyncio.sleep
    assert cf._SLEEP_MODE["fast"] is False, cf._SLEEP_MODE


def test_fast_mode_zeroes_sleeps_for_unmarked_tests():
    # The dispatcher is also what unmarked tests run under: fast mode on.
    assert cf._SLEEP_MODE["fast"] is True, cf._SLEEP_MODE
    assert asyncio.sleep is cf._dispatched_asyncio_sleep, asyncio.sleep


@pytest.mark.real_sleeps
async def test_deadline_refusal_survives_a_sibling_sleep_monkeypatch(monkeypatch):
    """The real victim shape, run AFTER a sibling patched asyncio.sleep: the
    dribble must really sleep, so the 0.05s deadline fires and refuses."""
    import crawlutron.api.middleware as mw

    monkeypatch.setattr(mw, "BODY_DEADLINE_SECONDS", 0.05)

    async def dribble() -> Message:
        started = time.monotonic()
        await asyncio.sleep(0.01)
        assert time.monotonic() - started >= 0.005, "asyncio.sleep did not sleep"
        return {"type": "http.request", "body": b"X", "more_body": True}

    request = make_request([], headers=[(b"content-length", b"100")], receive=dribble)

    async def probe():
        return await limit_request_body(
            request, refuse_never("refused bodies never reach the app"), max_bytes=1024
        )

    import asyncio as _a

    response = await _a.wait_for(probe(), timeout=5.0)
    assert response.status_code == 413
    assert response.headers["Connection"] == "close"
