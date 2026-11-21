"""Type definitions for the automated review system."""

from __future__ import annotations

from dataclasses import dataclass


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
