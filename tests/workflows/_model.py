"""The single typed projection of every workflow file on disk."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationInfo,
    field_validator,
    model_validator,
)

from tests._ci_contract import workflow_files
from tests.chokepoint_paths import REPO_ROOT
from tests.workflows._admission import admit_or_none as _admit_or_none
from tests.workflows._facts import CommandFacts, ExpressionFacts
from tests.workflows._names import (
    ActionRef,
    Cron,
    DisplayName,
    EnvName,
    Expression,
    ExpressionText,
    InputValue,
    JobId,
    StepId,
    StepName,
    TimeoutMinutes,
    WorkflowStem,
)
from tests.workflows._policy import CEILINGS, WAIVERS, Policy, waived_continue_on_error

STRICT = ConfigDict(extra="forbid", frozen=True)
_FACT_FIELDS = frozenset({"event_spliced", "command_facts"})
_ALIASES = {"with_": "with", "if_": "if", "continue_on_error": "continue-on-error"}


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


def _reject_construction(cls, *args: object, **kwargs: object) -> Step:
    raise TypeError("Step is admitted by validation, not construction")


class Step(BaseModel):
    model_config = STRICT
    name: StepName | None = None
    id: StepId | None = None
    uses: ActionRef | None = None
    run: ExpressionText | None = None
    with_: Mapping[str, InputValue] = Field(default_factory=dict, alias="with")
    env: Mapping[EnvName, InputValue] = Field(default_factory=dict)
    if_: Expression | None = Field(default=None, alias="if")
    shell: ExpressionText | None = None
    continue_on_error: bool | None = Field(default=None, alias="continue-on-error")
    line: int
    event_spliced: bool = False
    command_facts: CommandFacts = Field(default_factory=CommandFacts)

    @model_validator(mode="before")
    def strip_facts(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        return {key: value for key, value in data.items() if key not in _FACT_FIELDS}

    @model_validator(mode="after")
    def derive_facts(self) -> Step:
        run = self.run or ""
        object.__setattr__(self, "event_spliced", ExpressionFacts.create(run).references_event)
        object.__setattr__(self, "command_facts", CommandFacts.create(run, context={"env": dict(self.env)}))
        return self

    @classmethod
    def create(cls, raw: object, *, context: Mapping[str, object] | None = None) -> Step:
        return cls.model_validate(raw, context=context)

    @classmethod
    def admit_or_none(cls, raw: object, *, context: Mapping[str, object] | None = None) -> Step | None:
        return _admit_or_none(lambda value: cls.create(value, context=context), raw)

    @model_validator(mode="after")
    def codecov_inputs(self) -> Step:
        if self.uses and "codecov/codecov-action" in self.uses:
            bad = sorted(key for key in self.with_ if "-" in key)
            if bad:
                raise ValueError(f"invalid codecov input(s) {bad}: action inputs are snake_case")
        return self

    @model_validator(mode="after")
    def refusal(self, info: ValidationInfo) -> Step:
        waived = (info.context or {}).get("waived_lines", set())
        if self.continue_on_error and self.line not in waived:
            raise ValueError("continue-on-error may not be truthy")
        if self.uses and self.run:
            raise ValueError("a step either uses an action or runs a script")
        return self

    def model_copy(self, *, update: Mapping[str, object] | None = None, deep: bool = False) -> Step:
        raw = self.model_dump(by_alias=True)
        for key, value in (update or {}).items():
            raw[_ALIASES.get(key, key)] = value
        context = {"waived_lines": {self.line}} if self.continue_on_error else None
        return Step.create(raw, context=context)


_CONSTRUCTION_DOOR = "model_construct"
setattr(Step, _CONSTRUCTION_DOOR, classmethod(_reject_construction))


class Strategy(BaseModel):
    model_config = STRICT
    fail_fast: bool | None = Field(default=None, alias="fail-fast")
    max_parallel: int | None = Field(default=None, alias="max-parallel")
    matrix: Mapping[str, InputValue] = Field(default_factory=dict)


class Job(BaseModel):
    model_config = STRICT
    key: JobId
    workflow: WorkflowStem
    line: int
    name: DisplayName | None = None
    needs: tuple[JobId, ...] = ()
    if_: Expression | None = Field(default=None, alias="if")
    uses: ActionRef | None = None
    with_: Mapping[str, InputValue] = Field(default_factory=dict, alias="with")
    permissions: Mapping[PermissionScope, PermissionLevel] = Field(default_factory=dict)
    runs_on: Runner | None = Field(default=None, alias="runs-on")
    timeout_minutes: TimeoutMinutes | None = Field(default=None, alias="timeout-minutes")
    environment: InputValue | None = None
    outputs: Mapping[str, Expression] = Field(default_factory=dict)
    strategy: Strategy | None = None
    steps: tuple[Step, ...] = ()

    @classmethod
    def create(cls, raw: object, *, context: Mapping[str, object] | None = None) -> Job:
        return cls.model_validate(raw, context=context)

    @classmethod
    def admit_or_none(cls, raw: object, *, context: Mapping[str, object] | None = None) -> Job | None:
        return _admit_or_none(lambda value: cls.create(value, context=context), raw)

    @model_validator(mode="after")
    def call_or_run(self) -> Job:
        if bool(self.uses) == bool(self.steps):
            raise ValueError("a job either calls a workflow or has steps")
        if self.runs_on is not None and self.timeout_minutes is None:
            raise ValueError("timeout-minutes is required for a runner job")
        return self

    def step_named(self, name: str) -> Step:
        for step in self.steps:
            if step.name == name:
                return step
        raise AssertionError(f"job {self.workflow}:{self.key} has no step named {name!r}")


KNOWN_TRIGGERS = frozenset(
    {
        "branch_protection_rule",
        "pull_request",
        "push",
        "schedule",
        "workflow_call",
        "workflow_dispatch",
        "workflow_run",
    }
)


class Workflow(BaseModel):
    model_config = STRICT
    stem: WorkflowStem
    path: Path
    name: DisplayName
    on_: Mapping[str, InputValue] = Field(alias="on")
    permissions: Mapping[PermissionScope, PermissionLevel] = Field(default_factory=dict)
    concurrency: InputValue | None = None
    env: Mapping[EnvName, InputValue] = Field(default_factory=dict)
    defaults: InputValue | None = None
    run_name: ExpressionText | None = Field(default=None, alias="run-name")
    jobs: Mapping[JobId, Job] = Field(min_length=1)

    @classmethod
    def create(cls, raw: object, *, context: Mapping[str, object] | None = None) -> Workflow:
        return cls.model_validate(raw, context=context)

    @classmethod
    def admit_or_none(cls, raw: object, *, context: Mapping[str, object] | None = None) -> Workflow | None:
        return _admit_or_none(lambda value: cls.create(value, context=context), raw)

    @field_validator("on_")
    def known_triggers(cls, value: Mapping[str, object]) -> Mapping[str, object]:
        unknown = set(value) - KNOWN_TRIGGERS
        if unknown:
            raise ValueError(f"unknown workflow trigger(s): {sorted(unknown)}")
        schedule = value.get("schedule")
        if isinstance(schedule, list):
            for entry in schedule:
                if isinstance(entry, dict) and "cron" in entry:
                    TypeAdapter(Cron).validate_python(entry["cron"])
        return value


WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
PARSE_COUNT = 0


def _line_index(root) -> dict[tuple[str, ...], int]:
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

    if root is not None:
        walk(root, ())
    return found


def parse_document(path: Path) -> tuple[object, dict[tuple[str, ...], int]]:
    """The one YAML compose door: document and line index share a node tree."""
    loader = yaml.SafeLoader(path.read_text(encoding="utf-8"))
    root = loader.get_single_node()
    return loader.construct_document(root), _line_index(root)


def parse_workflow(path: Path) -> Workflow:
    """One read and one compose; line index and objects share the node tree."""
    global PARSE_COUNT
    PARSE_COUNT += 1
    raw, index = parse_document(path)
    if not isinstance(raw, dict):
        raise ValueError(f"{path.name} is not a workflow mapping")
    if True in raw:
        raw["on"] = raw.pop(True)
    stem = path.stem
    raw["stem"] = stem
    raw["path"] = path
    jobs = raw["jobs"]
    if not isinstance(jobs, dict):
        raise ValueError(f"{path.name} declares no jobs mapping")
    waived_lines: set[int] = set()
    for job_key, job in jobs.items():
        job["key"] = job_key
        job["workflow"] = stem
        job["line"] = index.get(("jobs", str(job_key)), 0)
        for number, step in enumerate(job.get("steps") or ()):
            step["line"] = index.get(("jobs", str(job_key), "steps", str(number)), 0)
            if "waived_continue_on_error" in step:
                raise ValueError(f"{path.name}:{step['line']}: waiver field is author-controlled")
            waived = waived_continue_on_error(stem, job_key, step.get("name"))
            if waived:
                if not step.get("continue-on-error"):
                    raise ValueError(f"{path.name}:{step['line']}: waiver without the violation")
                waived_lines.add(step["line"])
    return Workflow.create(raw, context={"waived_lines": waived_lines})


WORKFLOWS: tuple[Workflow, ...] = tuple(parse_workflow(path) for path in workflow_files(WORKFLOW_DIR))
JOBS: tuple[Job, ...] = tuple(job for item in WORKFLOWS for job in item.jobs.values())
POLICY = Policy.create(WAIVERS, context={"jobs": JOBS, "workflows": WORKFLOWS, "ceilings": CEILINGS})


def workflow(stem: WorkflowStem) -> Workflow:
    for item in WORKFLOWS:
        if item.stem == stem:
            return item
    raise AssertionError(f"no workflow named {stem!r} on disk")


def parent_workflow(job: Job) -> Workflow:
    return workflow(job.workflow)
