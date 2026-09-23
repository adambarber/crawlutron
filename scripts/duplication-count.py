#!/usr/bin/env python3
"""Report the exact duplicated-line count for a tree.

Every wave in this project must end with LESS duplication than it started with.
That obligation is easy to state and easy to fake: jscpd's console summary
rounds to one decimal place, so a wave that removes two clone lines and adds
one can still report the same percentage, and a reviewer comparing the summary
line has no way to tell.

This reads the JSON report instead, so the number is an exact integer that can
be compared before and after, and prints the delta when both are supplied:

    scripts/duplication-count.py app
    scripts/duplication-count.py app 211 27     # before: lines, clones

The token bound differs by tree on purpose. app/ is measured at 30 tokens
because duplication there is a second implementation of a property, which is
the defect class that survives review. tests/ is measured at 50 because at 30
it reports per-test setup that legitimately looks alike, and abstracting that
would hide what each test proves.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parent.parent

MIN_TOKENS = {"app": 30, "tests": 50}


def count(tree: str) -> tuple[int, int]:
    """Return (duplicated lines, clone pairs) for one tree."""
    with tempfile.TemporaryDirectory(prefix="duplication-count-") as raw:
        output = Path(raw)
        completed = subprocess.run(
            [
                "jscpd",
                "-f",
                "python",
                "--min-lines",
                "5",
                "--min-tokens",
                str(MIN_TOKENS[tree]),
                "--threshold",
                "100",
                "--reporters",
                "json",
                "--output",
                str(output),
                tree,
            ],
            cwd=SERVICE_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            print(completed.stdout + completed.stderr, file=sys.stderr)
            raise SystemExit(f"{tree}/: jscpd failed to run")
        total = json.loads((output / "jscpd-report.json").read_text())["statistics"]["total"]
        return int(total["duplicatedLines"]), int(total["clones"])


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 4) or argv[1] not in MIN_TOKENS:
        print(f"usage: {argv[0]} <{'|'.join(MIN_TOKENS)}> [before_lines before_clones]", file=sys.stderr)
        return 2
    lines, clones = count(argv[1])
    if len(argv) == 4:
        before_lines, before_clones = int(argv[2]), int(argv[3])
        delta = lines - before_lines
        verdict = "OK" if delta < 0 else ("NO CHANGE" if delta == 0 else "INCREASED")
        print(
            f"{argv[1]}/: {before_lines} -> {lines} duplicated lines "
            f"({delta:+d}), {before_clones} -> {clones} clones ({clones - before_clones:+d}) [{verdict}]"
        )
        return 0 if delta < 0 else 1
    print(f"{argv[1]}/: {lines} duplicated lines, {clones} clone pairs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
