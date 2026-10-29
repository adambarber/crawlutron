"""The sink a crawl writes spilled results to -- one declaration for its surface.

``write`` feeds the dispatch loops; ``load_ordered``/``close`` are the pipeline's
read-and-release on the same value. ``close`` conforms to the lifecycle protocol.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable
from typing import Protocol

from crawlutron.models.crawl_result import EngineCrawlResult
from crawlutron.models.host_policy import HostPolicy
from crawlutron.runtime.resource_lifecycle import ResourceLifecycle

__all__ = ["SpillSink", "write_spill"]


class SpillSink(Protocol):
    def write(self, index: int, result: EngineCrawlResult) -> Awaitable[None] | None: ...

    def load_ordered(self, *, policy: HostPolicy) -> list[EngineCrawlResult]: ...

    # close conforms to ResourceLifecycle: sync here, awaited-able for the async
    # implementers of the same declared shape.
    def close(self) -> None: ...


async def write_spill(spill: SpillSink | None, index: int, result: EngineCrawlResult) -> None:
    """Write a result to ``spill`` if present, awaiting the async ``write`` seam.

    A sync ``write`` returning None (a test double) needs no wait; ``None`` sink is a no-op.
    """
    if spill is None:
        return
    res = spill.write(index, result)
    if inspect.isawaitable(res):
        await res


_ = ResourceLifecycle  # the conformance contract this module's close declares
