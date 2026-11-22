"""The census proves uniformity: every workflow and job is governed."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tests.workflows._census import WAIVER_CEILINGS, missing_decisions, waiver_counts
from tests.workflows._model import JOBS, WORKFLOWS
from tests.workflows._registry import CATALOG, Decision, decision


def test_every_workflow_on_disk_has_a_named_decision() -> None:
    assert not missing_decisions(), f"workflows with no decision: {sorted(missing_decisions())}"
    assert set(CATALOG) == {item.stem for item in WORKFLOWS}


def test_a_workflow_with_no_decision_is_detected() -> None:
    assert missing_decisions(stems=("ghost-workflow",)) == {"ghost-workflow"}


def test_decision_ids_are_distinct_within_a_workflow() -> None:
    for stem, entries in CATALOG.items():
        ids = [kind for kind, _ in entries]
        assert len(ids) == len(set(ids)), f"{stem} registers a decision twice"


def test_every_job_either_runs_steps_or_calls_a_workflow() -> None:
    assert all(bool(job.steps) or bool(job.uses) for job in JOBS)


def test_waiver_tables_only_shrink() -> None:
    counts = waiver_counts()
    assert all(counts[name] <= ceiling for name, ceiling in WAIVER_CEILINGS.items())


def test_a_mistyped_workflow_or_decision_cannot_be_registered() -> None:
    with pytest.raises(ValidationError, match="unknown workflow"):
        decision("xcfb", Decision.FULL_MARKER_SET)
    typo = "XCFB"
    with pytest.raises(AttributeError):
        getattr(Decision, typo)
