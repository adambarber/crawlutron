"""Bound validated review verdicts before they reach GitHub."""

from typing import Final

from crawlutron.review.types import VerdictFinding

MAX_FINDINGS: Final[int] = 200
MAX_RATIONALE_LENGTH: Final[int] = 2_000
_TRUNCATION_MARKER: Final[str] = " [truncated]"


def truncate_rationale(rationale: str) -> str:
    if len(rationale) <= MAX_RATIONALE_LENGTH:
        return rationale
    keep = max(0, MAX_RATIONALE_LENGTH - len(_TRUNCATION_MARKER))
    return rationale[:keep].rstrip() + _TRUNCATION_MARKER


def cap_findings(findings: list[VerdictFinding]) -> list[VerdictFinding]:
    if len(findings) <= MAX_FINDINGS:
        return findings
    omitted = len(findings) - MAX_FINDINGS
    capped = findings[:MAX_FINDINGS]
    last = capped[-1]
    marker = f" [truncated: {omitted} findings omitted]"
    keep = max(0, MAX_RATIONALE_LENGTH - len(marker))
    capped[-1] = VerdictFinding(
        file=last.file,
        line=last.line,
        severity=last.severity,
        rule=last.rule,
        rationale=last.rationale[:keep].rstrip() + marker,
        confidence=last.confidence,
    )
    return capped
