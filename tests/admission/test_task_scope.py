"""TaskScope mechanism in isolation: spawn/stop/grace/refusal/named registry."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from crawlutron_admit import Count, Duration
from crawlutron_admit import task_scope as scope_module
from crawlutron_admit.task_scope import (
    Grace,
    Handle,
    NamedRegistry,
    RefusalReason,
    ScopeRefused,
    TaskScope,
)
from tests._support._doors import _GENERAL_ONE, _finish, _live
from tests._support._gate_factory import build_gate


async def _fresh():
    """A live grant, its exit door, and a scope riding the grant's lease."""
    gate, _ = build_gate()
    cm, granted = await _live(gate, _GENERAL_ONE)
    registry = NamedRegistry("ratelimit:anon:ip-address", Count(2), Duration(0.25))
    scope = TaskScope(granted=granted, registry=registry, grace=Grace(0.1))
    return cm, scope, gate, granted


async def _never() -> None:
    await asyncio.Event().wait()


async def _ignores_first_cancel(started: asyncio.Event) -> None:
    started.set()
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        await asyncio.Event().wait()


def test_spawn_returns_a_registered_task_handle():
    async def run() -> None:
        cm, scope, _, _ = await _fresh()
        try:
            handle = await scope.spawn(_never())
            assert isinstance(handle.task, asyncio.Task)
            assert handle.task in asyncio.all_tasks()
            assert scope.registry.active.value == 1
        finally:
            await scope.__aexit__(None, None, None)
            await _finish(cm)

    asyncio.run(run())


def test_stop_cancels_and_awaits_the_task():
    async def run() -> None:
        cm, scope, _, _ = await _fresh()
        try:
            handle = await scope.spawn(_never())
            await scope.stop(handle, Grace(0.1))
            assert handle.task.done()
            with pytest.raises(asyncio.CancelledError):
                handle.task.result()
            assert scope.registry.active.value == 0
        finally:
            await scope.__aexit__(None, None, None)
            await _finish(cm)

    asyncio.run(run())


def test_stop_grace_exceeded_refuses_by_name_without_hanging():
    async def run() -> None:
        cm, scope, _, _ = await _fresh()
        started = asyncio.Event()
        try:
            handle = await scope.spawn(_ignores_first_cancel(started))
            await asyncio.wait_for(started.wait(), 1.0)
            with pytest.raises(ScopeRefused) as caught:
                await asyncio.wait_for(scope.stop(handle, Grace(0.01)), 1.0)
            assert caught.value.reason is RefusalReason.GRACE_EXCEEDED
            assert isinstance(caught.value.limit, Count)
            assert isinstance(caught.value.retry_after, Duration)
            handle.task.cancel()
            await asyncio.wait_for(asyncio.gather(handle.task, return_exceptions=True), 1.0)
        finally:
            await scope.__aexit__(None, None, None)
            await _finish(cm)

    asyncio.run(run())


def test_stop_of_unregistered_target_refuses_with_typed_fields():
    async def run() -> None:
        cm, scope, _, _ = await _fresh()
        stray = asyncio.create_task(_never())
        foreign = Handle(id="never-registered", task=stray)
        try:
            with pytest.raises(ScopeRefused) as caught:
                await scope.stop(foreign, Grace(0.1))
            assert caught.value.reason is RefusalReason.TARGET_NOT_REGISTERED
            assert caught.value.limit == scope.registry.limit
            assert caught.value.retry_after == scope.registry.retry_after
        finally:
            stray.cancel()
            await asyncio.wait_for(asyncio.gather(stray, return_exceptions=True), 1.0)
            await scope.__aexit__(None, None, None)
            await _finish(cm)

    asyncio.run(run())


def test_repeated_stop_of_known_handle_is_idempotent():
    async def run() -> None:
        cm, scope, _, _ = await _fresh()
        try:
            handle = await scope.spawn(_never())
            await scope.stop(handle, Grace(0.1))
            await scope.stop(handle, Grace(0.1))
            assert scope.registry.active.value == 0
        finally:
            await scope.__aexit__(None, None, None)
            await _finish(cm)

    asyncio.run(run())


def test_scope_exit_stops_all_then_releases_lease():
    async def run() -> None:
        cm, scope, _, granted = await _fresh()
        first = await scope.spawn(_never())
        second = await scope.spawn(_never())
        await scope.__aexit__(None, None, None)
        assert first.task.done() and second.task.done()
        assert first.task.cancelled() and second.task.cancelled()
        assert scope.registry.active.value == 0
        assert granted.lease.release() is False
        await _finish(cm)

    asyncio.run(run())


def test_named_registry_refuses_at_its_own_limit():
    registry = NamedRegistry("ratelimit:anon:ip-address", Count(1), Duration(0.5))
    registry.admit()
    with pytest.raises(ScopeRefused) as caught:
        registry.admit()
    assert caught.value.reason is RefusalReason.RETRY_PRESSURE
    assert caught.value.limit == Count(1)
    assert caught.value.retry_after == Duration(0.5)
    registry.release()
    registry.admit()


def test_grace_is_bounded_finite_and_nonnegative():
    with pytest.raises(ValueError):
        Grace(-0.1)
    with pytest.raises(ValueError):
        Grace(float("inf"))
    with pytest.raises(ValueError):
        Grace(float("nan"))
    with pytest.raises(ValueError):
        Grace(1e9)
    assert Grace(0.0).value == 0.0


def test_refusal_is_never_empty():
    refusal = ScopeRefused(
        reason=RefusalReason.TARGET_NOT_REGISTERED,
        limit=Count(1),
        retry_after=Duration(0.25),
    )
    assert refusal.reason is RefusalReason.TARGET_NOT_REGISTERED
    assert refusal.limit == Count(1)
    assert refusal.retry_after == Duration(0.25)
    assert str(refusal)


def test_module_stays_pure_and_io_free():
    source = Path(scope_module.__file__).read_text()
    assert "from crawlutron import" not in source
    assert "import crawlutron\n" not in source
    assert "from crawlutron." not in source
    for banned in ("sqlite3", "socket", "aiohttp", "pathlib", "open("):
        assert banned not in source
