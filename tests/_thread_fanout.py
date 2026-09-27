"""The N-way thread fan-out a concurrency test needs, written once.

Four modules -- the cookie jar, the DNS cache, the HTTP validator cache and the
bounded TTL cache -- each had a "many threads hit this at once" test, and each
had spelled the same eight lines: build the threads, start them all, join them
all, and re-raise whatever the workers caught. That is the duplication, and it
is a trap as well: the version that asserts inside the worker collects the
failure into a list and the version that does not lose it, and a test whose
worker raised is a test that silently proved nothing unless someone remembered
the final ``assert not errors``.

``run_thread_fanout`` is the fan-out. The worker is handed its own index and
owns everything else it needs by closure; whatever it raises -- including an
``AssertionError`` from inside it -- comes back as the returned list, so a test
that forgets to assert still cannot pass quietly, because the list is what it
asserts on.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

__all__ = ["run_thread_fanout"]


def run_thread_fanout(worker: Callable[[int], None], *, count: int = 8) -> list[BaseException]:
    """Run ``worker(index)`` on ``count`` threads at once; return what raised.

    The threads are started before any is joined, so the overlap the test is
    about actually happens; every thread is joined even if one raised, so a
    failure cannot leave a worker mutating the cache the assertions then read.
    An empty list means every worker finished clean -- which is the assertion
    each caller makes on it.
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
