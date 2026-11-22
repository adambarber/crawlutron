"""The graph checks are proved to fail on broken calls, cycles and inputs."""

from __future__ import annotations

import pytest

from tests.workflows import test_graph_integrity as graph
from tests.workflows._model import JOBS, WORKFLOWS, workflow


def test_a_ghost_reusable_call_is_caught(monkeypatch) -> None:
    job = next(job for job in JOBS if job.uses and graph._CALLS in job.uses)
    broken = job.model_copy(update={"uses": "$/.github/workflows/ghost.yml"})
    monkeypatch.setattr(graph, "JOBS", (broken,))
    with pytest.raises(AssertionError, match="missing workflow"):
        graph.test_every_local_reusable_call_resolves_to_a_workflow_on_disk()


def test_a_cycle_is_caught(monkeypatch) -> None:
    first = workflow("ci")
    second = workflow("released")
    call_a = next(iter(first.jobs.values())).model_copy(
        update={"uses": "$/.github/workflows/b.yml", "steps": ()}
    )
    call_b = next(iter(second.jobs.values())).model_copy(
        update={"uses": "$/.github/workflows/a.yml", "steps": ()}
    )
    a = first.model_copy(update={"stem": "a", "jobs": {"caller": call_a}})
    b = second.model_copy(update={"stem": "b", "jobs": {"caller": call_b}})
    monkeypatch.setattr(graph, "WORKFLOWS", (a, b))
    with pytest.raises(AssertionError, match="cycle"):
        graph.test_the_call_graph_has_no_cycle()


def test_an_omitted_required_input_is_caught(monkeypatch) -> None:
    job = next(job for job in JOBS if job.uses and "released.yml" in (job.uses or ""))
    broken = job.model_copy(update={"with_": {}})
    monkeypatch.setattr(graph, "JOBS", (broken,))
    monkeypatch.setattr(graph, "WORKFLOWS", WORKFLOWS)
    with pytest.raises(AssertionError, match="does not pass input"):
        graph.test_every_caller_passes_the_inputs_the_callee_declares()
