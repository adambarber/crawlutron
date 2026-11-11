"""Releasing pooled clients across loop boundaries.

`discard_if_unowned_loop` decides where a superseded client's close runs: on
its owning loop (bound and open), on a fresh loop, on a background thread's
loop, or as a tracked task when the caller itself runs on the owning loop.
The tracked-task and dead-loop paths are the structural fix for the unraisable
``_SelectorTransport.__del__`` flake family (#820 unit 3): a close no loop
will ever run leaves the socket to warn at a random later test's GC.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable, Coroutine
from typing import Protocol, cast

from crawlutron.runtime import process_state

__all__ = [
    "PooledClient",
    "PENDING_CLIENT_CLOSE",
    "PENDING_CLIENT_CLOSE_LOCK",
    "clear_live_loop_close_tasks",
    "clear_pending_client_closes",
    "close_on_fresh_loop",
    "close_on_thread",
    "discard_if_unowned_loop",
    "drain_pending_client_closes",
    "safe_aclose",
]


class PooledClient(Protocol):
    """What every pooled kind answers to: close handles and the loop binding the
    pool installs. httpx closes through ``aclose``; curl_cffi through ``close``.
    """

    def close(self) -> object: ...
    async def aclose(self) -> object: ...


# Superseded loop-bound pooled clients awaiting a loop to close on. The queue
# is the one shared backlog across every release helper, so it is public: the
# pool modules read and clear it through the reset doors below.
PENDING_CLIENT_CLOSE: list[PooledClient] = []
PENDING_CLIENT_CLOSE_LOCK = threading.Lock()

# Closes scheduled onto the loop that is currently running (a reset called
# from inside the owning loop): the teardown drains these before the loop
# closes.
_LIVE_LOOP_CLOSE_TASKS: set[asyncio.Task[None]] = set()


async def safe_aclose(client: PooledClient) -> None:
    """Await a pooled client's async close, recording any failure in the #643
    decline record -- the client is being replaced, so a close error is non-fatal.
    """
    from crawlutron.runtime.resource_lifecycle import record_declined_close

    try:
        # curl_cffi's AsyncSession closes through ``close()`` (a coroutine),
        # httpx through ``aclose()``; either handle satisfies the await here.
        close = client.aclose if hasattr(client, "aclose") else client.close
        await cast(Awaitable[object], close())
    except Exception:
        record_declined_close("aclose_failed", type(client).__name__)


def close_on_fresh_loop(close: Callable[[], Awaitable[object]]) -> None:
    """Close on a fresh loop, torn down after; the previous current loop is
    restored so a closed loop is never left as this thread's current loop."""
    try:
        previous = asyncio.get_event_loop()
    except RuntimeError:
        previous = None
    fresh_loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(fresh_loop)
        fresh_loop.run_until_complete(close())
    finally:
        fresh_loop.close()
        if previous is not None:
            asyncio.set_event_loop(previous)
        else:
            asyncio.set_event_loop(None)


def close_on_thread(close: Callable[[], Awaitable[object]]) -> None:
    """Close on a daemon thread's fresh loop when another loop runs here.

    BaseException is captured and re-raised to the caller; a wait that never
    wakes within 30s raises TimeoutError instead of returning silently.
    """
    exc: list[BaseException | None] = [None]
    done = threading.Event()

    def _run() -> None:
        try:
            fresh_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(fresh_loop)
            try:
                fresh_loop.run_until_complete(close())
            finally:
                fresh_loop.close()
        except BaseException as e:  # surface close failures to the caller
            exc[0] = e
        finally:
            done.set()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    if not done.wait(timeout=30):
        raise TimeoutError("close on background thread did not complete within 30s")
    if exc[0] is not None:
        raise exc[0]


def _bound_loop_of(client: PooledClient) -> asyncio.AbstractEventLoop | None:
    """The loop the client is bound to, via its attribute seam, else None."""
    return getattr(client, "_bound_loop", None)


def _close_captured_and_queue(client: PooledClient) -> Coroutine[object, object, None]:
    """Close the captured client, then empty the pending queue.

    The queue empties inside the same task, or a sync reset later would host
    a fresh loop on a thread whose loop still runs.
    """

    async def _run() -> None:
        await safe_aclose(client)
        while True:
            with PENDING_CLIENT_CLOSE_LOCK:
                if not PENDING_CLIENT_CLOSE:
                    return
                stale = PENDING_CLIENT_CLOSE.pop(0)
            await safe_aclose(stale)

    return _run()


def _schedule_on_owning_loop(bound: asyncio.AbstractEventLoop, client: PooledClient) -> None:
    """Schedule the captured client's close on the loop the caller runs on.

    Blocking would deadlock the loop that must run the close (the 30s wait
    always times out, stranding the socket for GC -- the unraisable ``__del__``
    flake family). The close is a tracked task; the teardown drains it before
    the loop closes.
    """
    task: asyncio.Task[None] = bound.create_task(_close_captured_and_queue(client))
    _LIVE_LOOP_CLOSE_TASKS.add(task)
    task.add_done_callback(_LIVE_LOOP_CLOSE_TASKS.discard)


def _close_on_owning_loop(bound: asyncio.AbstractEventLoop, close: Callable[[], Awaitable[object]]) -> None:
    """Close on a bound loop: scheduled-and-awaited when it runs on another
    thread, driven directly when it is open but idle."""
    if bound.is_running():
        coro = cast(Coroutine[object, object, object], close())
        asyncio.run_coroutine_threadsafe(coro, bound).result(timeout=30)
    else:
        bound.run_until_complete(close())


def _close_cross_loop(close: Callable[[], Awaitable[object]]) -> None:
    """Close when the client has no open owning loop.

    A loop may be RUNNING on this thread even with no current loop -- hosting
    a fresh loop there raises "another loop is running", so use a background
    thread in that case.
    """
    try:
        running = asyncio.get_running_loop() is not None
    except RuntimeError:
        running = False
    if running:
        close_on_thread(close)
    else:
        close_on_fresh_loop(close)


def discard_if_unowned_loop(
    close: Callable[[], Awaitable[object]], slot: str, namespace: dict[str, PooledClient | None]
) -> None:
    """Release ``slot``'s object, closing it on its owning loop when it has one.

    ``namespace`` holds the pooled object; one helper serves every pool module.
    A bound, open loop owns the close; a None or closed bound loop falls
    through to the cross-loop branches.
    """
    client = namespace[slot]
    if client is None:
        return
    bound = _bound_loop_of(client)
    if bound is not None and not bound.is_closed():
        # A real pooled client is loop-bound: close on its loop instead of
        # whichever loop happens to be current at teardown.
        if bound.is_running():
            try:
                mine = asyncio.get_running_loop()
            except RuntimeError:
                mine = None
            if mine is bound:
                _schedule_on_owning_loop(bound, client)
            else:
                _close_on_owning_loop(bound, close)
        else:
            bound.run_until_complete(close())
        namespace[slot] = None
        return
    _close_cross_loop(close)
    namespace[slot] = None


async def drain_pending_client_closes() -> None:
    """Close superseded queued clients and join this loop's own close tasks."""
    while True:
        with PENDING_CLIENT_CLOSE_LOCK:
            if not PENDING_CLIENT_CLOSE:
                break
            client = PENDING_CLIENT_CLOSE.pop(0)
        await safe_aclose(client)
    # Join the close tasks this loop itself scheduled (a reset called from
    # inside the owning loop): they own the pooled transports. The task
    # draining right now (a close task re-entering here) is excluded, or the
    # gather would wait on itself forever.
    # The join is also the backstop under the reset abort net: _reset_release_state
    # reclaims sockets stranded by a task that dies with its loop.
    me = asyncio.current_task()
    while pending := {
        t for t in _LIVE_LOOP_CLOSE_TASKS if t is not me and t.get_loop() is asyncio.get_running_loop()
    }:
        await asyncio.gather(*pending, return_exceptions=True)
        await asyncio.sleep(0)


def clear_live_loop_close_tasks() -> None:
    """Reset door: drop every tracked live-loop close task.

    The tasks are done or cancelled by the time a reset runs; the set itself
    is module state the reset family must be able to empty.
    """
    _LIVE_LOOP_CLOSE_TASKS.clear()


def _reset_release_state() -> None:
    """Release this module's reset-shaped state.

    A still-pending tracked task belongs to a loop that may be gone, so its
    sockets are aborted loop-free first; dropping the task alone would strand
    the socket for GC (the unraisable __del__ family).
    """
    from crawlutron.transport.pooled_socket_abort import abort_transport_sockets

    loops: list[asyncio.AbstractEventLoop] = []
    for client in tuple(PENDING_CLIENT_CLOSE):
        bound = _bound_loop_of(client)
        if bound is not None and bound.is_closed():
            loops.append(bound)
    abort_transport_sockets(tuple(loops))
    _LIVE_LOOP_CLOSE_TASKS.clear()
    with PENDING_CLIENT_CLOSE_LOCK:
        PENDING_CLIENT_CLOSE.clear()


def clear_pending_client_closes() -> None:
    """Reset door: empty the pending-close queue without closing anything.

    The reset family releases the queued clients through the drain first;
    this door exists so tests can isolate the queue state itself.
    """
    with PENDING_CLIENT_CLOSE_LOCK:
        PENDING_CLIENT_CLOSE.clear()


def _has_pending_client_state() -> bool:
    """Whether the release module still holds queued clients or close tasks."""
    with PENDING_CLIENT_CLOSE_LOCK:
        queued = bool(PENDING_CLIENT_CLOSE)
    return queued or bool(_LIVE_LOOP_CLOSE_TASKS)


# The release module owns its own reset-shaped state (queue + tracked tasks);
# registering it here keeps the reset family exhaustive (the reset-shaped-state
# invariant refuses an unregistered reset door).
process_state.register(__name__, _reset_release_state, _has_pending_client_state)
