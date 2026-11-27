"""Seeded interleaving harness: pooled clients -> egress proxy -> job lifecycle
-> throttle -> webhook/watch. Only the pooled-client owner is implemented here;
the next owner adds its actions to this driver instead of forking another one.
"""

from __future__ import annotations

import asyncio
import random
import socket
import sys
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from types import TracebackType

from crawlutron.config.settings import CrawlerSettings
from crawlutron.runtime import process_state
from crawlutron.transport import http_client_pool
from crawlutron.transport.pooled_clients import PooledClientBuilder, bind_to_running_loop
from crawlutron_admit.task_scope import Grace, Handle, NamedRegistry, TaskScope
from crawlutron_admit.values import Count, Duration
from tests._async_harnesses import run_loop_rebind_scenario

__all__ = ["Action", "HarnessViolation", "InterleavingOracle", "Trace", "drive"]

_MAX_STEPS = 18


class Action(StrEnum):
    """The six lifecycle actions one interleaving step may choose."""

    BUILD = "build_client"
    START = "start_task"
    DRAIN = "cancel_drain"
    RESET = "reset"
    REBIND = "adopt_rebind_loop"
    CLOSE = "close_loop"


_ACTIONS = tuple(Action)


@dataclass(frozen=True)
class HarnessViolation:
    """One observed breach: the channel and the evidence it captured."""

    channel: str
    detail: str


@dataclass(frozen=True)
class Trace:
    """The replayable action names and the violations they produced."""

    actions: tuple[str, ...]
    violations: tuple[HarnessViolation, ...]


class _Client:
    """A pooled-client double holding one live socket until it is closed."""

    def __init__(self) -> None:
        self.closed = False
        self._socket = socket.socket()

    @property
    def is_closed(self) -> bool:
        """The predicate the production release path reads."""
        return self.closed

    @property
    def live(self) -> bool:
        """Whether a live transport socket still survives."""
        return self._socket.fileno() != -1

    async def aclose(self) -> None:
        """Close through the same door the httpx pooled client uses."""
        await asyncio.sleep(0)
        self.close()

    def close(self) -> None:
        """The sync close handle the pooled-client protocol declares."""
        self._socket.close()
        self.closed = True

    def dispose(self) -> None:
        """Last-resort cleanup so a planted leak never leaks a descriptor."""
        self.close()


class InterleavingOracle:
    """Capture warnings, unraisables, pending tasks and live sockets."""

    def __init__(self) -> None:
        self._captured: list[warnings.WarningMessage] = []
        self._unraisable: list[str] = []
        self._catcher: warnings.catch_warnings | None = None
        self._previous_hook = sys.unraisablehook

    def __enter__(self) -> InterleavingOracle:
        self._catcher = warnings.catch_warnings(record=True)
        self._captured = self._catcher.__enter__()
        warnings.simplefilter("always")
        sys.unraisablehook = self._collect_unraisable
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        sys.unraisablehook = self._previous_hook
        if self._catcher is not None:
            self._catcher.__exit__(exc_type, exc, traceback)

    def _collect_unraisable(self, args: sys.UnraisableHookArgs) -> None:
        """Record a real unraisable instead of letting the interpreter print it."""
        self._unraisable.append(f"{args.exc_type.__name__}: {args.exc_value}")

    def observe(
        self,
        *,
        tasks: tuple[asyncio.Task[None], ...] = (),
        clients: tuple[_Client, ...] = (),
    ) -> tuple[HarnessViolation, ...]:
        """Snapshot all four channels; empty means the lifecycle is clean."""
        seen = tuple(self._captured)
        self._captured.clear()
        raisables = tuple(self._unraisable)
        self._unraisable.clear()
        found = [HarnessViolation("warning", str(item.message)) for item in seen]
        found.extend(HarnessViolation("unraisable", text) for text in raisables)
        found.extend(HarnessViolation("pending_task", repr(task)) for task in tasks if not task.done())
        found.extend(HarnessViolation("live_socket", repr(client)) for client in clients if client.live)
        return tuple(found)


def _builder(built: list[_Client]) -> PooledClientBuilder:
    def build(settings: CrawlerSettings | None = None) -> _Client:
        client = _Client()
        built.append(client)
        return bind_to_running_loop(client)

    return build


def _scope() -> TaskScope:
    registry = NamedRegistry("test:interleaving", Count(32), Duration(1.0))
    return TaskScope.process(registry=registry, grace=Grace(1.0))


def _build_step(loop: asyncio.AbstractEventLoop, guard: float) -> None:
    async def build() -> None:
        http_client_pool.get_async_http_client()

    loop.run_until_complete(asyncio.wait_for(build(), guard))


def _start_step(
    loop: asyncio.AbstractEventLoop, scope: TaskScope, handles: list[Handle], guard: float
) -> None:
    async def start() -> None:
        handles.append(scope.spawn_now(lambda: asyncio.sleep(guard / 10)))

    loop.run_until_complete(asyncio.wait_for(start(), guard))


def _drain_step(
    loop: asyncio.AbstractEventLoop, scope: TaskScope, handles: list[Handle], guard: float
) -> None:
    async def drain() -> None:
        for handle in handles:
            await scope.stop(handle, Grace(guard))

    loop.run_until_complete(asyncio.wait_for(drain(), guard))
    handles.clear()


def _rebind_step(loop: asyncio.AbstractEventLoop, guard: float) -> None:
    scenario = run_loop_rebind_scenario(http_client_pool.get_async_http_client, guard=guard)
    loop.run_until_complete(asyncio.wait_for(scenario, guard))


def _apply(
    loop: asyncio.AbstractEventLoop,
    action: Action,
    scope: TaskScope,
    handles: list[Handle],
    release: Callable[[], None],
    guard: float,
) -> None:
    if action is Action.BUILD:
        _build_step(loop, guard)
    elif action is Action.START:
        _start_step(loop, scope, handles, guard)
    elif action is Action.DRAIN:
        _drain_step(loop, scope, handles, guard)
    elif action is Action.RESET:
        _drain_step(loop, scope, handles, guard)
        release()
    elif action is Action.REBIND:
        _rebind_step(loop, guard)


def drive(
    seed: int,
    steps: int = 12,
    *,
    reset: Callable[[], None] | None = None,
    guard: float = 1.0,
) -> Trace:
    """Run one bounded, seeded interleaving and return its replayable trace."""
    release = reset if reset is not None else process_state.reset_all_process_state
    rng = random.Random(seed)
    scope = _scope()
    built: list[_Client] = []
    handles: list[Handle] = []
    actions: list[str] = []
    violations: list[HarnessViolation] = []
    with InterleavingOracle() as oracle:
        with http_client_pool.pooled_client_builders(http=_builder(built)):
            loop = asyncio.new_event_loop()
            loops = [loop]
            try:
                for _ in range(min(steps, _MAX_STEPS)):
                    action = rng.choice(_ACTIONS)
                    actions.append(action.value)
                    if action is Action.CLOSE:
                        _drain_step(loop, scope, handles, guard)
                        loop.close()
                        loop = asyncio.new_event_loop()
                        loops.append(loop)
                    else:
                        _apply(loop, action, scope, handles, release, guard)
                    if action is Action.RESET:
                        violations.extend(
                            oracle.observe(tasks=tuple(h.task for h in handles), clients=tuple(built))
                        )
                _drain_step(loop, scope, handles, guard)
                release()
                violations.extend(oracle.observe(tasks=tuple(h.task for h in handles), clients=tuple(built)))
            finally:
                for client in built:
                    client.dispose()
                for owned in loops:
                    if not owned.is_closed():
                        owned.close()
    return Trace(tuple(actions), tuple(violations))
