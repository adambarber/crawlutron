"""The N-way thread fan-out a concurrency test needs, written once: it starts and
joins all threads and returns whatever raised, so several modules no longer each
spell the same launch-and-join loops.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

__all__ = ["run_thread_fanout", "run_threads"]


def _launch_and_join(threads: list[threading.Thread]) -> None:
    """Start every thread before joining any (true overlap), then join all of them."""
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


def run_threads(target: Callable[[], None], *, count: int = 8) -> list[threading.Thread]:
    """Start ``count`` ``threading.Thread(target=target)`` workers and join all of
    them, returning the threads for any post-hoc inspection."""
    threads = [threading.Thread(target=target) for _ in range(count)]
    _launch_and_join(threads)
    return threads


def run_thread_fanout(worker: Callable[[int], None], *, count: int = 8) -> list[BaseException]:
    """Run ``worker(index)`` on ``count`` threads at once; start before joining so the
    overlap happens, join all even if one raised, and return what raised -- an empty
    list means the clean pass every caller asserts on.
    """
    errors: list[BaseException] = []

    def _run(index: int) -> None:
        try:
            worker(index)
        except BaseException as exc:  # collected and asserted in the main thread
            errors.append(exc)

    threads = [threading.Thread(target=_run, args=(index,), name=f"fanout-{index}") for index in range(count)]
    _launch_and_join(threads)
    return errors
