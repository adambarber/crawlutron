"""EffectContainer: bounded effect execution with per-kind semaphores."""

from __future__ import annotations

import asyncio

import pytest

from crawlutron.service.effects import (
    EffectBounds,
    EffectContainer,
    EffectKind,
    EffectOutcome,
)


def _bounds(caller: float) -> EffectBounds:
    return EffectBounds(
        timeout_seconds=0.0,
        caller_timeout_seconds=caller,
        floor_seconds=0.0,
        min_seconds=0.0,
    )


async def _sleep_forever() -> None:
    await asyncio.sleep(10.0)


def test_derive_timeout_caller_zero_min_wins() -> None:
    assert (
        EffectContainer.derive_timeout(caller_timeout_seconds=0.0, floor_seconds=5.0, min_seconds=10.0)
        == 10.0
    )


def test_derive_timeout_large_caller_caller_plus_floor_wins() -> None:
    assert (
        EffectContainer.derive_timeout(caller_timeout_seconds=100.0, floor_seconds=5.0, min_seconds=10.0)
        == 105.0
    )


def test_derive_timeout_floor_zero_caller_plus_min_logic() -> None:
    assert (
        EffectContainer.derive_timeout(caller_timeout_seconds=3.0, floor_seconds=0.0, min_seconds=10.0)
        == 10.0
    )


def test_derive_timeout_caller_below_min_min_wins() -> None:
    assert (
        EffectContainer.derive_timeout(caller_timeout_seconds=2.0, floor_seconds=5.0, min_seconds=10.0)
        == 10.0
    )


@pytest.mark.parametrize(
    ("caller", "floor", "minimum", "expected"),
    [
        (0.0, 5.0, 10.0, 10.0),
        (3.0, 5.0, 10.0, 10.0),
        (6.0, 5.0, 10.0, 11.0),
        (100.0, 5.0, 10.0, 105.0),
        (0.0, 0.0, 0.0, 0.0),
    ],
)
def test_derive_timeout_max_formula(caller: float, floor: float, minimum: float, expected: float) -> None:
    assert (
        EffectContainer.derive_timeout(
            caller_timeout_seconds=caller, floor_seconds=floor, min_seconds=minimum
        )
        == expected
    )


async def test_run_times_out_returns_timed_out() -> None:
    container = EffectContainer(concurrency_per_kind={EffectKind.FETCH: 1})

    result = await container.run(
        EffectKind.FETCH,
        _sleep_forever,
        caller_timeout_seconds=0.05,
        bounds=_bounds(0.05),
    )
    assert result.outcome is EffectOutcome.TIMED_OUT
    assert result.kind is EffectKind.FETCH
    assert result.error_code == "effect_timeout"


async def test_run_success_path() -> None:
    container = EffectContainer(concurrency_per_kind={EffectKind.FETCH: 1})

    async def _work() -> int:
        await asyncio.sleep(0.01)
        return 7

    result = await container.run(
        EffectKind.FETCH,
        _work,
        caller_timeout_seconds=1.0,
        bounds=_bounds(1.0),
    )
    assert result.outcome is EffectOutcome.SUCCESS
    assert result.elapsed_seconds >= 0.0


async def test_run_releases_semaphore_after_cancellation() -> None:
    container = EffectContainer(concurrency_per_kind={EffectKind.FETCH: 1})

    task = asyncio.create_task(
        container.run(
            EffectKind.FETCH,
            _sleep_forever,
            caller_timeout_seconds=1.0,
            bounds=_bounds(1.0),
        )
    )
    await asyncio.sleep(0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    # A second run must acquire immediately: the slot was released.
    result = await container.run(
        EffectKind.FETCH,
        lambda: asyncio.sleep(0),
        caller_timeout_seconds=1.0,
        bounds=_bounds(1.0),
    )
    assert result.outcome is EffectOutcome.SUCCESS


async def test_fetch_door_records_elapsed() -> None:
    container = EffectContainer(concurrency_per_kind={EffectKind.FETCH: 8})
    result = await container.fetch(
        url="https://example.com/",
        timeout_seconds=0.01,
        caller_timeout_seconds=1.0,
    )
    assert result.kind is EffectKind.FETCH
    assert result.outcome is EffectOutcome.SUCCESS
    assert result.elapsed_seconds >= 0.0
