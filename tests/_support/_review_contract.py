"""Typed contract for the advisory review scripts, on the declared test support surface."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypedDict


class ProbeResultRecord(TypedDict):
    """One probe's recorded evidence, written by the probes job."""

    name: str
    argv: list[str]
    exit_code: int
    duration_ms: int
    stdout_path: str
    stderr_path: str
    stdout_sha256: str
    stderr_sha256: str


def probe_name(record: ProbeResultRecord) -> str:
    return record["name"]


def probe_argv(record: ProbeResultRecord) -> list[str]:
    return record["argv"]


def probe_exit_code(record: ProbeResultRecord) -> int:
    return record["exit_code"]


def probe_duration_ms(record: ProbeResultRecord) -> int:
    return record["duration_ms"]


def probe_stdout_path(record: ProbeResultRecord) -> str:
    return record["stdout_path"]


def probe_stderr_path(record: ProbeResultRecord) -> str:
    return record["stderr_path"]


def probe_stdout_sha256(record: ProbeResultRecord) -> str:
    return record["stdout_sha256"]


def probe_stderr_sha256(record: ProbeResultRecord) -> str:
    return record["stderr_sha256"]


@dataclass(frozen=True, slots=True)
class ProbeArtifact:
    """One structured probe artifact."""

    name: str
    result: ProbeResultRecord
    data: dict[str, object]


@dataclass(frozen=True, slots=True)
class VerdictFinding:
    """One source-grounded review finding."""

    file: str
    line: int
    severity: str
    rule: str
    rationale: str
    confidence: str


@dataclass(frozen=True, slots=True)
class Verdict:
    """A validated automated-review verdict."""

    verdict: str
    findings: tuple[VerdictFinding, ...]
    summary: str


@dataclass(frozen=True, slots=True)
class AgentDefinition:
    """One review agent's model, prompt, probes, and severity threshold."""

    name: str
    model: str
    prompt: str
    probes: tuple[str, ...]
    severity_threshold: str


@dataclass(frozen=True, slots=True)
class ProbeDefinition:
    """One allowlisted probe command and its description."""

    name: str
    command: str
    description: str


@dataclass(frozen=True, slots=True)
class RoutingRule:
    """A path or label rule that selects a registered agent."""

    selector: str
    agent: str


@dataclass(frozen=True, slots=True)
class ReviewRegistry:
    """The complete review policy loaded from one TOML registry."""

    default_agent: str
    agents: dict[str, AgentDefinition]
    probes: dict[str, ProbeDefinition]
    path_rules: tuple[RoutingRule, ...]
    label_rules: tuple[RoutingRule, ...]
