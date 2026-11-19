"""The sleep recorder observes the thread that installed it: a leaked sibling
thread's sleeps are not this test's requests."""

import asyncio
import threading

from tests._clocks import recorder


def _sleep_in_another_thread(delay: float) -> threading.Thread:
    async def sleeper() -> None:
        await asyncio.sleep(delay)

    thread = threading.Thread(target=lambda: asyncio.run(sleeper()), daemon=True)
    thread.start()
    thread.join(5)
    return thread


def test_another_threads_sleep_is_not_recorded(monkeypatch) -> None:
    sleeps = recorder(monkeypatch)
    thread = _sleep_in_another_thread(0.5)
    assert not thread.is_alive(), "the sleeping thread never finished"
    assert sleeps.delays == [], f"another thread's sleep was recorded: {sleeps.delays}"


def test_the_installing_threads_sleep_is_recorded(monkeypatch) -> None:
    sleeps = recorder(monkeypatch)
    _ = asyncio.run(asyncio.sleep(0.25))
    assert sleeps.delays == [0.25]
