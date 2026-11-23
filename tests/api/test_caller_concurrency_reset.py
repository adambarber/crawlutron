"""Concurrency-tracker reset clears planted reservations between tests."""

import asyncio

from crawlutron.runtime import process_state
from crawlutron.service.caller_concurrency import CallerConcurrencyTracker


async def test_reset_clears_planted_concurrency_reservation():
    """A reservation planted via the public door is cleared by reset_all_process_state."""
    tracker = CallerConcurrencyTracker()

    process_state.register("test-caller-concurrency-bite", tracker.reset, tracker.has_state)
    try:
        await tracker.reserve("", max_concurrency=4, already_active=0)
        assert await tracker.sync_in_flight("") > 0

        process_state.reset_all_process_state()

        assert await tracker.sync_in_flight("") == 0
    finally:
        process_state.unregister("test-caller-concurrency-bite")


async def test_concurrent_async_crawl_admission_releases_reservation():
    """Three concurrent admissions through admit() leave no in-process reservation."""
    tracker = CallerConcurrencyTracker()

    process_state.register("test-caller-concurrency-bite", tracker.reset, tracker.has_state)
    try:

        async def admit_one() -> str:
            from crawlutron.service.caller_auth import Tracking, admit, anonymous_caller

            async with admit(
                anonymous_caller(),
                0,
                tracking=Tracking(job_store=None, concurrency_tracker=tracker),
            ) as owner:
                return owner

        await asyncio.gather(*(admit_one() for _ in range(3)))

        assert await tracker.sync_in_flight("") == 0, "admission path left a reservation"
    finally:
        process_state.unregister("test-caller-concurrency-bite")


__all__ = [
    "test_concurrent_async_crawl_admission_releases_reservation",
    "test_reset_clears_planted_concurrency_reservation",
]
