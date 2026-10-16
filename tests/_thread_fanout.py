"""The N-way thread fan-out a concurrency test needs, written once: it starts and
joins all threads and returns whatever raised, so four modules no longer each
spell the same eight lines and diverge on whether a worker's failure surfaces.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

__all__ = ["run_thread_fanout"]


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
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return errors
