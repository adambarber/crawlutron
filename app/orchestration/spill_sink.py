"""The sink a crawl writes spilled results to -- one declaration for its surface.

``write`` feeds the dispatch loops; ``load_ordered``/``close`` are the pipeline's
read-and-release on the same value, so one protocol names all three.
"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Protocol

from app.models.crawl_result import EngineCrawlResult
from app.models.host_policy import HostPolicy


class _SpillSink(Protocol):
    def write(self, index: int, result: EngineCrawlResult) -> Awaitable[None] | None: ...

    def load_ordered(self, *, policy: HostPolicy) -> list[EngineCrawlResult]: ...

    def close(self) -> None: ...
