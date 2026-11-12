"""Shared RAM-sizing arrangement for the seam's kernel-parity tests."""

from __future__ import annotations

from crawlutron.runtime.pressure_sizing import TaskRamBudget, build_admission_policy

__all__ = ["seam_sizing_budget", "seam_sizing_policy"]


def seam_sizing_budget() -> TaskRamBudget:
    """The doc-worker budget the seam sizing tests share."""
    return TaskRamBudget(doc_workers=2)


def seam_sizing_policy():
    """A 3 GiB box with a 2-browser pool and 3 concurrent pages: 22 HTTP slots."""
    return build_admission_policy(
        3 * 1024 * 1024 * 1024,
        pool_size=2,
        max_concurrent_pages=3,
        budget=seam_sizing_budget(),
    )
