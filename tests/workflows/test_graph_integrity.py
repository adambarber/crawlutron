"""The reusable-workflow graph resolves and its output chain is wired."""

from __future__ import annotations

import re

from tests.chokepoint_paths import REPO_ROOT
from tests.workflows._model import JOBS, WORKFLOWS, Workflow, parent_workflow

_CALLS = "$/.github/workflows/"
_ACTIONS = "$/.github/actions/"


def _callee(job) -> Workflow | None:
    if not job.uses or _CALLS not in job.uses:
        return None
    stem = job.uses.rsplit("/", 1)[-1].split("@", 1)[0].removesuffix(".yml")
    return next((item for item in WORKFLOWS if item.stem == stem), None)


def test_every_local_reusable_call_resolves_to_a_workflow_on_disk() -> None:
    calls = [job for job in JOBS if job.uses and _CALLS in job.uses]
    assert calls, "no reusable calls found: the graph check would pass vacuously"
    for job in calls:
        assert _callee(job) is not None, f"{job.workflow}:{job.key} calls a missing workflow"


def test_every_local_action_resolves_to_a_directory() -> None:
    steps = [step for job in JOBS for step in job.steps if step.uses and _ACTIONS in step.uses]
    assert steps, "no local actions found: the path check would pass vacuously"
    for step in steps:
        uses = step.uses or ""
        target = uses.split("@", 1)[0].removeprefix("$/")
        assert (REPO_ROOT / target).is_dir(), f"missing local action {uses}"


def test_the_call_graph_has_no_cycle() -> None:
    edges = {
        item.stem: tuple(callee.stem for job in item.jobs.values() if (callee := _callee(job)) is not None)
        for item in WORKFLOWS
    }
    assert any(edges.values()), "an empty graph cannot fail a cycle check"
    visiting: set[str] = set()
    done: set[str] = set()

    def visit(stem: str) -> None:
        if stem in done:
            return
        if stem in visiting:
            raise AssertionError(f"cycle through {stem}")
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
            step_ref = producer.outputs.get(match.group(2), "")
            assert re.fullmatch(
                r"\$\{\{ steps\.[A-Za-z0-9_-]+\.outputs\.[A-Za-z0-9_-]+ \}\}", str(step_ref)
            ), f"{item.stem}: {output} never reaches a step output"


def test_every_caller_passes_the_inputs_the_callee_declares() -> None:
    checked = 0
    for job in JOBS:
        callee = _callee(job)
        if callee is None:
            continue
        checked += 1
        declared = callee.on_.get("workflow_call")
        inputs = declared.get("inputs", {}) if isinstance(declared, dict) else {}
        for name in inputs:
            assert name in job.with_, f"{job.workflow}:{job.key} does not pass input {name}"
    assert checked, "no caller/callee pairs were checked"


def test_every_job_in_the_model_has_a_parent_workflow() -> None:
    assert all(parent_workflow(job).stem == job.workflow for job in JOBS)
