"""The sensor Protocol the gate consumes, with the fail-closed doctrine (7).

A gauge is healthy / unhealthy / cold / stale; raising gauges are hard-saturated
and nothing propagates to the admit caller.
"""

from __future__ import annotations

import enum
from typing import Protocol, runtime_checkable

from admission.values import Ratio

__all__ = ["GaugeState", "ResourceGauge"]


class GaugeState(enum.StrEnum):
    """The explicit gauge classification (§7.2)."""

    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    COLD = "cold"
    STALE = "stale"


@runtime_checkable
class ResourceGauge(Protocol):
    """A sensor whose usage gates a resource.

    ``is_warm`` feeds the cold/stale classification; ``read_usage`` delivers a
    measured fraction or raises (fail-closed); ``last_sample_age`` parks a stale box.
    """

    def is_warm(self) -> bool:
        """Whether this resource has a trustworthy, current sample."""
        ...

    def read_usage(self) -> Ratio:
        """The current measured usage as a fraction; raises when unhealthy."""
        ...

    def last_sample_age(self, clock_now: float) -> float:
        """Seconds since the last sample, on the kernel's injected clock."""
        ...
