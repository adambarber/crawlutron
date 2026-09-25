import asyncio


async def run_loop_rebind_scenario(get_client, delay: float = 0.05):
    first = get_client()
    foreign = asyncio.new_event_loop()
    try:
        first._bound_loop = foreign
        second = get_client()
        await asyncio.sleep(delay)
    finally:
        foreign.close()
    return first, second


__all__ = ["run_loop_rebind_scenario"]
