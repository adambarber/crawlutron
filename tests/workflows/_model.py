"""The single typed projection of every workflow file on disk."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from tests.chokepoint_paths import REPO_ROOT
from tests.workflows._names import (
    ActionRef,
    DisplayName,
    EnvName,
    Expression,
    JobId,
    StepId,
    StepName,
    TimeoutMinutes,
    WorkflowStem,
)

STRICT = ConfigDict(extra="forbid", frozen=True)


class Runner(StrEnum):
    UBUNTU_24_04 = "ubuntu-24.04"


class PermissionScope(StrEnum):
    CONTENTS = "contents"
    PULL_REQUESTS = "pull-requests"
    ID_TOKEN = "id-token"
    PACKAGES = "packages"
    ATTESTATIONS = "attestations"
    CHECKS = "checks"
    SECURITY_EVENTS = "security-events"
    ACTIONS = "actions"


class PermissionLevel(StrEnum):
    READ = "read"
    WRITE = "write"
    NONE = "none"


@dataclass(frozen=True)
class Waiver:
    workflow: str
    job: str
    step: str
    reason: str


CONTINUE_ON_ERROR_WAIVERS = (
    Waiver(
        "agent-review",
        "review",
        "Create GitHub App token",
        "A fail-closed guard step follows; the token is advisory, not the gate",
    ),
)


class Step(BaseModel):
    model_config = STRICT
    name: StepName | None = None
    id: StepId | None = None
    uses: ActionRef | None = None
    run: str | None = None
    with_: Mapping[str, object] = Field(default_factory=dict, alias="with")
    env: Mapping[EnvName, object] = Field(default_factory=dict)
    if_: Expression | None = Field(default=None, alias="if")
    shell: str | None = None
    continue_on_error: bool | None = Field(default=None, alias="continue-on-error")
    waived_continue_on_error: bool = False
    line: int

    @model_validator(mode="after")
    def refusal(self) -> Step:
        assert not self.continue_on_error or self.waived_continue_on_error, (
            "continue-on-error may not be truthy"
        )
        assert not (self.uses and self.run), "a step either uses an action or runs a script"
        return self


class Strategy(BaseModel):
    model_config = STRICT
    fail_fast: bool | None = Field(default=None, alias="fail-fast")
    max_parallel: int | None = Field(default=None, alias="max-parallel")
    matrix: Mapping[str, object] = Field(default_factory=dict)


class Job(BaseModel):
    model_config = STRICT
    key: JobId
    workflow: WorkflowStem
    line: int
    name: DisplayName | None = None
    needs: tuple[JobId, ...] = ()
    if_: Expression | None = Field(default=None, alias="if")
    uses: ActionRef | None = None
    with_: Mapping[str, object] = Field(default_factory=dict, alias="with")
    permissions: Mapping[PermissionScope, PermissionLevel] = Field(default_factory=dict)
    runs_on: Runner | None = Field(default=None, alias="runs-on")
    timeout_minutes: TimeoutMinutes | None = Field(default=None, alias="timeout-minutes")
    environment: object | None = None
    outputs: Mapping[str, Expression] = Field(default_factory=dict)
    strategy: Strategy | None = None
    steps: tuple[Step, ...] = ()

    @model_validator(mode="after")
    def call_or_run(self) -> Job:
        assert bool(self.uses) != bool(self.steps), "a job either calls a workflow or has steps"
        return self

    def step_named(self, name: str) -> Step:
        for step in self.steps:
            if step.name == name:
                return step
        raise AssertionError(f"job {self.workflow}:{self.key} has no step named {name!r}")


class Workflow(BaseModel):
    model_config = STRICT
    stem: WorkflowStem
    path: Path
    name: DisplayName
    on_: Mapping[str, object] = Field(alias="on")
    permissions: Mapping[PermissionScope, PermissionLevel] = Field(default_factory=dict)
    concurrency: object | None = None
    jobs: Mapping[JobId, Job] = Field(min_length=1)


WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
PARSE_COUNT = 0


def _line_index(text: str) -> dict[tuple[str, ...], int]:
    """Key paths to their YAML line, from the composed node tree."""
    found: dict[tuple[str, ...], int] = {}

    def walk(node, path: tuple[str, ...]) -> None:
        if isinstance(node, yaml.MappingNode):
            for key_node, value_node in node.value:
                key = str(key_node.value)
                found[(*path, key)] = key_node.start_mark.line + 1
                walk(value_node, (*path, key))
        elif isinstance(node, yaml.SequenceNode):
            for index, item in enumerate(node.value):
                found[(*path, str(index))] = item.start_mark.line + 1
                walk(item, (*path, str(index)))

    root = yaml.compose(text)
    if root is not None:
        walk(root, ())
    return found


def parse_workflow(path: Path) -> Workflow:
    """The one read + one parse of a workflow file, for the whole suite."""
    global PARSE_COUNT
    PARSE_COUNT += 1
    text = path.read_text(encoding="utf-8")
    raw = yaml.safe_load(text)
    assert isinstance(raw, dict), f"{path.name} is not a workflow mapping"
    if True in raw:
        raw["on"] = raw.pop(True)
    stem = path.stem
    index = _line_index(text)
    raw["stem"] = stem
    raw["path"] = path
    jobs = raw["jobs"]
    assert isinstance(jobs, dict)
    for job_key, job in jobs.items():
        job["key"] = job_key
        job["workflow"] = stem
        job["line"] = index.get(("jobs", str(job_key)), 0)
        for number, step in enumerate(job.get("steps") or ()):
            step["line"] = index.get(("jobs", str(job_key), "steps", str(number)), 0)
            name = step.get("name")
            waived = any(
                w.workflow == stem and w.job == job_key and w.step == name for w in CONTINUE_ON_ERROR_WAIVERS
            )
            if waived:
                step["waived_continue_on_error"] = True
    return Workflow.model_validate(raw)


WORKFLOWS: tuple[Workflow, ...] = tuple(parse_workflow(path) for path in sorted(WORKFLOW_DIR.glob("*.yml")))
JOBS: tuple[Job, ...] = tuple(job for item in WORKFLOWS for job in item.jobs.values())


def workflow(stem: WorkflowStem) -> Workflow:
    for item in WORKFLOWS:
        if item.stem == stem:
            return item
    raise AssertionError(f"no workflow named {stem!r} on disk")


def parent_workflow(job: Job) -> Workflow:
    return workflow(job.workflow)
