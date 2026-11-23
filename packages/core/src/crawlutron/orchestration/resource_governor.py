"""The one process admission chokepoint (swappable for a distributed coordinator)."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING, Protocol

from crawlutron.runtime import process_state
from crawlutron.runtime.pressure_sizing import (
    TaskRamBudget,
    build_admission_gate,
    build_admission_policy,
    derive_browser_pool_size,
    doc_worker_count,
)
from crawlutron.vocabulary.limits import DEFAULT_MAX_CONCURRENT_PAGES
from crawlutron_admit import Granted, Want
from crawlutron_admit.sensing.pressure_gates import ResourceGate
from crawlutron_admit.sensing.pressure_memory import read_memory_stats

if TYPE_CHECKING:
    from crawlutron_admit import AdmissionGate

__all__ = [
    "ResourceGovernor",
    "build_resource_governor",
    "install_resource_governor",
    "resource_governor",
]


class ResourceGovernor(Protocol):
    """The single admission door; the lease stays live until the caller exits it."""

    def admit(self, want: Want) -> AbstractAsyncContextManager[Granted]: ...


def build_resource_governor(gate: ResourceGate) -> AdmissionGate:
    """Derive the one RAM budget once, from the process gauge.

    The ceiling is the fixed process default, never a caller's
    max_concurrent_pages: one shared chokepoint cannot be resized by one crawl.
    """
    budget = TaskRamBudget(doc_workers=doc_worker_count())
    return build_admission_gate(
        gate,
        build_admission_policy(
            read_memory_stats().total_bytes or 0,
            pool_size=derive_browser_pool_size(),
            max_concurrent_pages=DEFAULT_MAX_CONCURRENT_PAGES,
            budget=budget,
        ),
        budget=budget,
    )


_governor: ResourceGovernor | None = None


def resource_governor() -> ResourceGovernor:
    """The one process-wide governor, derived from RAM on its first consultation."""
    global _governor
    if _governor is None:
        _governor = build_resource_governor(ResourceGate())
    return _governor


def install_resource_governor(governor: ResourceGovernor) -> None:
    """Swap the governor (a cross-process coordinator) without touching call sites."""
    global _governor
    _governor = governor


def _reset() -> None:
    global _governor
    _governor = None


def _has() -> bool:
    return _governor is not None


process_state.register(__name__, _reset, _has)
