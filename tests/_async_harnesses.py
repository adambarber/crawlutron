import asyncio

from _timing_harnesses import await_until


async def run_loop_rebind_scenario(get_client, *, guard: float | None = None):
    """Rebind a pooled client onto a foreign loop and let its eager close run.

    The scenario used to end in ``await asyncio.sleep(delay)`` -- a fixed
    pause whose only job was to give the teardown the code had just scheduled a
    chance to run. That is a bet, not an assertion: on a loaded box the close
    may not have started yet, and the test still passed because it only checked
    the flag afterwards. Every pooled client shape in the suite publishes
    ``closed``, so the wait is on *that*: the scenario returns once the close
    the code scheduled has actually completed, and returns immediately if it
    never was scheduled at all.
    """
    first = get_client()
    foreign = asyncio.new_event_loop()
    try:
        first._bound_loop = foreign
        second = get_client()
        kwargs = {} if guard is None else {"guard": guard}
        await await_until(
            lambda: first.closed,
            "the superseded client's eager close never ran",
            **kwargs,
        )
    finally:
        foreign.close()
    return first, second


__all__ = ["run_loop_rebind_scenario"]
