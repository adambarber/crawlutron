"""Commit-msg gate, two laws, one chokepoint (#561).

Citation law: every commit cites `#<number>`. Format law: a Conventional
Commit subject, `type(scope): ...` with type in _TYPES (scope optional).
Schema law: a breaking-feat commit touching crawl_policy.py must bump
POLICY_SCHEMA_VERSION in the same commit. Refusals name which law fired.
"""

from __future__ import annotations

import re
import subprocess
import sys

_TYPES = ("feat", "fix", "docs", "chore", "refactor", "test", "build", "ci", "perf")
_SUBJECT = re.compile(rf"^({'|'.join(_TYPES)})(\([^()\s]+\))?!?: \S")
_CITATION = re.compile(r"#\d+\b")
_BREAKING_FEAT = re.compile(r"^feat(\([^()\s]+\))?!:", re.MULTILINE)
_POLICY_PATH = "crawlutron/models/crawl_policy.py"
_ADDED_DECL = re.compile(r'^\+POLICY_SCHEMA_VERSION\s*=\s*"([^"]*)"', re.MULTILINE)
_REMOVED_DECL = re.compile(r'^-POLICY_SCHEMA_VERSION\s*=\s*"([^"]*)"', re.MULTILINE)
_DIFF_CMD = ("git", "diff", "--name-only", "HEAD")
_PATCH_CMD = ("git", "diff", "HEAD", "--", _POLICY_PATH)


def _diff_paths() -> list[str]:
    try:
        out = subprocess.run(_DIFF_CMD, capture_output=True, text=True, check=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return out.split()


def _schema_bumped() -> bool:
    try:
        out = subprocess.run(_PATCH_CMD, capture_output=True, text=True, check=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    added, removed = _ADDED_DECL.findall(out), _REMOVED_DECL.findall(out)
    return bool(added) and (not removed or added[-1] != removed[-1])


def main(argv: list[str]) -> int:
    """Fail when the message breaks a law; the refusal names the law."""
    if len(argv) != 1:
        print("usage: python -m scripts.commit_citation_gate <commit-msg-file>", file=sys.stderr)
        return 2
    try:
        message = open(argv[0], encoding="utf-8").read()
    except OSError as error:
        print(f"commit_citation_gate: cannot read {argv[0]}: {error}", file=sys.stderr)
        return 2
    if not _CITATION.search(message):
        print(
            f"commit_citation_gate: {argv[0]} violates the CITATION LAW: it cites no issue; "
            "add 'Closes #<number>' (repo rule: every commit cites an issue)",
            file=sys.stderr,
        )
        return 1
    subject = message.splitlines()[0] if message.splitlines() else ""
    if not _SUBJECT.search(subject):
        print(
            f"commit_citation_gate: {argv[0]} violates the COMMIT-FORMAT LAW: subject "
            f"'{subject}' is not a Conventional Commit; use 'type(scope): ...' with type in "
            f"{', '.join(_TYPES)} (scope optional; #561)",
            file=sys.stderr,
        )
        return 1
    if _BREAKING_FEAT.search(subject) and _POLICY_PATH in _diff_paths() and not _schema_bumped():
        print(
            f"commit_citation_gate: {argv[0]} violates the SCHEMA LAW: a breaking feat "
            f"touching {_POLICY_PATH} must bump POLICY_SCHEMA_VERSION in the same commit (#561)",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
