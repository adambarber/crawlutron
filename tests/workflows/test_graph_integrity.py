"""The reusable-workflow graph resolves and its output chain is wired."""

from __future__ import annotations

import re

from tests.workflows._model import JOBS, WORKFLOWS, Workflow, parent_workflow

_CALLS = "#/.github/workflows/"


def _callee(job) -> Workflow | None:
    if not job.uses or _CALLS not in job.uses:
        return None
    stem = job.uses.rsplit("/", 1)[-1]
    return next((item for item in WORKFLOWS if item.stem == stem), None)


def test_every_local_reusable_call_resolves_to_a_workflow_on_disk() -> None:
    for job in JOBS:
        if job.uses and job.uses.startswith(("$/", "./")) and _CALLS not in job.uses:
            continue
        if job.uses and _CALLS in job.uses:
            assert _callee(job) is not None, f"{job.workflow}:{job.key} calls a missing workflow"


def test_the_call_graph_has_no_cycle() -> None:
    edges = {
        item.stem: tuple(callee.stem for job in item.jobs.values() if (callee := _callee(job)) is not None)
        for item in WORKFLOWS
    }
    visiting: set[str] = set()
    done: set[str] = set()

    def visit(stem: str) -> None:
        if stem in done:
            return
        assert stem not in visiting, f"cycle through {stem}"
        visiting.add(stem)
        for target in edges[stem]:
            visit(target)
        visiting.remove(stem)
        done.add(stem)

    for stem in edges:
        visit(stem)


def test_every_called_output_maps_through_a_job_output_to_a_step() -> None:
    for item in WORKFLOWS:
        called = item.on_.get("workflow_call")
        if not isinstance(called, dict):
            continue
        outputs = called.get("outputs")
        if not isinstance(outputs, dict):
            continue
        for output, spec in outputs.items():
            assert isinstance(spec, dict)
            value = str(spec.get("value", ""))
            match = re.fullmatch(r"\$\{\{ jobs\.([A-Za-z0-9_-]+)\.outputs\.([A-Za-z0-9_-]+) \}\}", value)
            assert match, f"{item.stem}: output {output} does not map from a job output"
            producer = item.jobs.get(match.group(1))
            assert producer is not None, f"{item.stem}: no job {match.group(1)}"
            assert match.group(2) in producer.outputs, f"{item.stem}: no output on the producer"


def test_every_caller_passes_the_inputs_the_callee_declares() -> None:
    for job in JOBS:
        callee = _callee(job)
        if callee is None:
            continue
        declared = callee.on_.get("workflow_call")
        inputs = declared.get("inputs", {}) if isinstance(declared, dict) else {}
        for name, spec in inputs.items():
            if isinstance(spec, dict) and spec.get("default") is not None:
                continue
            assert name in job.with_, f"{job.workflow}:{job.key} does not pass required input {name}"


def test_every_job_in_the_model_has_a_parent_workflow() -> None:
    assert all(parent_workflow(job).stem == job.workflow for job in JOBS)
