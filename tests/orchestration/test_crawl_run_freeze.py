"""The run carrier's immutability law: config is fixed at run start.

A stage that rewrites a run constant (depth cap, page budget, gate) would silently change every later
batch; the frozen carrier makes that a defect at the write site instead of a mystery in the results.
"""

from __future__ import annotations

import dataclasses

import pytest

from crawlutron.orchestration.recursive import CrawlRun
from tests._admission_runs import minimum_admission_run

__all__ = ["test_run_config_is_frozen_for_the_whole_run"]


def test_run_config_is_frozen_for_the_whole_run() -> None:
    run, _state, _admission = minimum_admission_run([], http_quota=1, browser_quota=1)
    assert isinstance(run, CrawlRun)
    # A direct `run.max_pages = ...` anywhere is a mypy read-only error; the
    # runtime bite of removing frozen is mutation-proven (scratch tree).
    assert "max_pages" in CrawlRun.__slots__
    with pytest.raises(dataclasses.FrozenInstanceError):
        run.__setattr__("max_pages", 0)  # the unbounded sentinel, smuggled mid-run
