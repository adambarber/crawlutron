"""Bounded effect execution container with per-kind semaphores. Claim-oblivious."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, TypeVar, cast

if TYPE_CHECKING:
    from crawlutron.storage.job_store import JobStore


class EffectKind(StrEnum):
    FETCH = "fetch"
    RENDER = "render"
    DB_WRITE = "db_write"
    WEBHOOK_POST = "webhook_post"
    SUBPROCESS_SPAWN = "subprocess_spawn"


class EffectOutcome(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    TIMED_OUT = "timed_out"
    CRASHED = "crashed"


@dataclass(frozen=True)
class EffectResult:
    kind: EffectKind
    outcome: EffectOutcome
    error: str | None = None
    error_code: str | None = None
    elapsed_seconds: float = 0.0
    retries: int = 0


@dataclass(frozen=True)
class EffectBounds:
    timeout_seconds: float
    caller_timeout_seconds: float
    floor_seconds: float
    min_seconds: float
    max_retries: int = 0


_DEFAULT_CONCURRENCY: dict[EffectKind, int] = {
    EffectKind.FETCH: 8,
    EffectKind.DB_WRITE: 1,
    EffectKind.WEBHOOK_POST: 4,
    EffectKind.SUBPROCESS_SPAWN: 2,
}

_T = TypeVar("_T")


async def _sleep_op(timeout_seconds: float) -> object:
    await asyncio.sleep(timeout_seconds)
    return None


class EffectContainer:
    """One semaphore per effect kind; defaults: FETCH=8, DB_WRITE=1,
    WEBHOOK_POST=4, SUBPROCESS_SPAWN=2, RENDER=browser pool size."""

    def __init__(self, *, concurrency_per_kind: dict[EffectKind, int] | None = None) -> None:
        concurrency = dict(_DEFAULT_CONCURRENCY)
        from crawlutron.runtime.pressure_sizing import derive_browser_pool_size

        concurrency.setdefault(EffectKind.RENDER, derive_browser_pool_size())
        if concurrency_per_kind is not None:
            concurrency.update(concurrency_per_kind)
        self._semaphores: dict[EffectKind, asyncio.Semaphore] = {
            kind: asyncio.Semaphore(max(1, width)) for kind, width in concurrency.items()
        }

    @staticmethod
    def derive_timeout(*, caller_timeout_seconds: float, floor_seconds: float, min_seconds: float) -> float:
        return max(caller_timeout_seconds + floor_seconds, min_seconds)

    def _semaphore(self, kind: EffectKind) -> asyncio.Semaphore:
        return self._semaphores[kind]

    async def run(
        self,
        kind: EffectKind,
        op: Callable[[], Awaitable[object]],
        *,
        caller_timeout_seconds: float,
        bounds: EffectBounds | None = None,
    ) -> EffectResult:
        """Run ``op`` under ``kind``'s semaphore and a derived timeout bound."""
        if bounds is None:
            bounds = EffectBounds(
                timeout_seconds=0.0,
                caller_timeout_seconds=caller_timeout_seconds,
                floor_seconds=0.0,
                min_seconds=0.0,
            )
        timeout = self.derive_timeout(
            caller_timeout_seconds=caller_timeout_seconds,
            floor_seconds=bounds.floor_seconds,
            min_seconds=bounds.min_seconds,
        )
        semaphore = self._semaphore(kind)
        start = time.monotonic()
        async with semaphore:
            try:
                await asyncio.wait_for(op(), timeout=timeout)
            except TimeoutError:
                return EffectResult(
                    kind=kind,
                    outcome=EffectOutcome.TIMED_OUT,
                    error="effect timed out",
                    error_code="effect_timeout",
                    elapsed_seconds=time.monotonic() - start,
                    retries=bounds.max_retries,
                )
            except asyncio.CancelledError:
                raise
        return EffectResult(
            kind=kind,
            outcome=EffectOutcome.SUCCESS,
            elapsed_seconds=time.monotonic() - start,
            retries=bounds.max_retries,
        )

    async def fetch(
        self,
        *,
        url: str,
        timeout_seconds: float,
        op: Callable[[], Awaitable[object]] | None = None,
        caller_timeout_seconds: float | None = None,
        bounds: EffectBounds | None = None,
    ) -> EffectResult:
        return await self.run(
            EffectKind.FETCH,
            op if op is not None else (lambda: _sleep_op(timeout_seconds)),
            caller_timeout_seconds=caller_timeout_seconds or timeout_seconds,
            bounds=bounds,
        )

    async def render(
        self,
        *,
        page_op: Callable[[], Awaitable[object]],
        bounds: EffectBounds | None = None,
        timeout_seconds: float,
        caller_timeout_seconds: float | None = None,
    ) -> EffectResult:
        return await self.run(
            EffectKind.RENDER,
            page_op,
            bounds=bounds,
            caller_timeout_seconds=caller_timeout_seconds or timeout_seconds,
        )

    async def db_write(
        self,
        txn: Callable[[], _T],
        *,
        owner: str = "",
        timeout_seconds: float = 30.0,
    ) -> _T:
        """Run sync ``txn`` off the loop under the DB_WRITE semaphore (width 1).

        ``owner`` is for tracing; the container is claim-oblivious. ``txn`` runs via
        ``asyncio.to_thread`` so the writer thread has no running loop.
        """
        async with self._semaphore(EffectKind.DB_WRITE):
            return await asyncio.wait_for(asyncio.to_thread(txn), timeout=timeout_seconds)

    async def webhook_post(
        self,
        *,
        target: object,
        payload: object,
        bounds: EffectBounds | None = None,
        caller_timeout_seconds: float | None = None,
        op: Callable[[], Awaitable[object]] | None = None,
        timeout_seconds: float,
    ) -> EffectResult:
        _ = target
        _ = payload
        return await self.run(
            EffectKind.WEBHOOK_POST,
            (lambda: _sleep_op(timeout_seconds)) if op is None else op,
            caller_timeout_seconds=caller_timeout_seconds or timeout_seconds,
            bounds=bounds,
        )

    async def subprocess_spawn(
        self,
        *,
        cmd: list[str],
        timeout_seconds: float,
        caller_timeout_seconds: float | None = None,
        bounds: EffectBounds | None = None,
    ) -> EffectResult:
        _ = cmd
        return await self.run(
            EffectKind.SUBPROCESS_SPAWN,
            lambda: _sleep_op(timeout_seconds),
            bounds=bounds,
            caller_timeout_seconds=caller_timeout_seconds or timeout_seconds,
        )


#: Shared effect container bounding all in-flight effects across callers.
_DEFAULT_CONTAINER = EffectContainer()


async def run_collect_batch(
    container: EffectContainer,
    op: Callable[[], Awaitable[object]],
    url: str,
    timeout_seconds: float,
    caller_timeout_seconds: float,
) -> list[object]:
    """Run ``op`` under the container's fetch bound, collecting outputs."""
    outputs: list[object] = []

    async def _collect() -> None:
        outputs.append(await op())

    await container.fetch(
        url=url,
        op=_collect,
        timeout_seconds=timeout_seconds,
        caller_timeout_seconds=caller_timeout_seconds,
    )
    return outputs


async def run_crawl_with_claim(
    container: EffectContainer,
    op: Callable[[], Awaitable[object]],
    url: str,
    timeout: float,
    job_store: JobStore,
    job_id: str,
    token: str,
) -> tuple[list[object], bool]:
    """Run a crawl under the container bound and renew the claim."""
    if not isinstance(timeout, (int, float)):
        timeout = 30.0
    outputs = await run_collect_batch(container, op, url, timeout, timeout)
    still_owned = await job_store.arenew_job_claim(job_id, token, now=time.time())
    first = cast("dict[str, object]", outputs[0]) if outputs else {}
    items = cast("list[object]", first.get("results", [])) if isinstance(first, dict) else []
    return items, still_owned


async def container_effect[EffectT](
    container: EffectContainer | None,
    kind: EffectKind,
    op: Callable[[], Awaitable[EffectT]],
    *,
    timeout_seconds: float,
) -> EffectT:
    """Run ``op`` under ``container``'s per-kind bound and return its value.

    The effect's return value is captured through a closure because
    ``EffectContainer.run`` returns the ``EffectResult``, not the op's output.
    """
    container = container or _DEFAULT_CONTAINER
    result_holder: list[EffectT] = []

    async def _capture() -> None:
        result_holder.append(await op())

    if kind is EffectKind.RENDER:
        await container.render(
            page_op=_capture,
            timeout_seconds=timeout_seconds,
            caller_timeout_seconds=timeout_seconds,
        )
    else:
        await container.fetch(
            url="",
            timeout_seconds=timeout_seconds,
            op=_capture,
            caller_timeout_seconds=timeout_seconds,
        )
    return result_holder[0]
