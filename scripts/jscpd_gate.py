"""A duplication gate that reports every duplicate at the floor and fails on any.

There is no exemption surface: no content baseline, no import-skip. A duplicate is a
finding; eliminating it is the only way through. The floor is the one knob.
"""

from __future__ import annotations

import argparse
import json
import re
import sys

import yaml

from scripts import _jscpd_pair_resolution as resolver
from scripts._jscpd_report_types import JscpdDuplicate

# Re-exports: the contract surface the gate's own suite reads off this module.
CONFIG_PATH = resolver.CONFIG_PATH
PRECOMMIT_CONFIG_PATH = resolver.PRECOMMIT_CONFIG_PATH
PROFILE_PATHS = resolver.PROFILE_PATHS
SANCTIONED_IGNORE_GLOBS = resolver.SANCTIONED_IGNORE_GLOBS
SERVICE_ROOT = resolver.SERVICE_ROOT
TREE_ROOTS = resolver.TREE_ROOTS
# jscpd_report is the ONE invocation of jscpd; the sweep feeds it a root and a
# floor and gets back the parsed report -- contract surface, not resolver detail.
jscpd_report = resolver.jscpd_report


def configured_floor() -> tuple[int, int, float]:
    """The hook's own (min-lines, min-tokens, threshold), read as one triple."""
    """The floor the pre-commit hook actually runs at, read from the hook itself.

    ``.pre-commit-config.yaml`` is the threshold source; :func:`_require_configured_floor`
    refuses a laxer floor, so the gate cannot be a keystroke away from hiding clones.
    """
    document = yaml.safe_load(resolver.PRECOMMIT_CONFIG_PATH.read_text(encoding="utf-8"))
    hooks = [
        hook
        for repo_hooks in document.get("repos", ())
        for hook in repo_hooks.get("hooks", ())
        if hook.get("id") == "jscpd"
    ]
    if len(hooks) != 1:
        raise ValueError(f"expected exactly one jscpd hook to read a floor from, found {len(hooks)}")
    entry = hooks[0]["entry"]
    numbers = {}
    for flag in ("min-lines", "min-tokens", "threshold"):
        match = re.search(rf"--{flag}\s+(\S+)", entry)
        if match is None:
            raise ValueError(f"the jscpd hook entry declares no --{flag}, so the floor is unreadable")
        numbers[flag] = match.group(1)
    return int(numbers["min-lines"]), int(numbers["min-tokens"]), float(numbers["threshold"])


def _require_configured_floor(args: argparse.Namespace) -> None:
    """Refuse a run whose thresholds are laxer than the hook's.

    Only laxer is rejected: scanning *stricter* is how a new floor gets measured
    before adoption -- that is the point of running the gate by hand -- so it is allowed.
    """
    min_lines, min_tokens, threshold = configured_floor()
    laxer = (args.min_lines > min_lines, args.min_tokens > min_tokens, args.threshold > threshold)
    if any(laxer):
        raise ValueError(
            f"scanning at ({args.min_lines} lines / {args.min_tokens} tokens / {args.threshold}%) is "
            f"laxer than the enforced floor ({min_lines} / {min_tokens} / {threshold}%) in "
            f"{resolver.PRECOMMIT_CONFIG_PATH.name}: a clean result would only mean the clone is "
            f"below the number asked for. Scan stricter to measure a candidate floor."
        )


__all__ = [
    "CONFIG_PATH",
    "PRECOMMIT_CONFIG_PATH",
    "PROFILE_PATHS",
    "SANCTIONED_IGNORE_GLOBS",
    "SERVICE_ROOT",
    "TREE_ROOTS",
    "jscpd_report",
]

MIN_SOURCES = {"all": 200}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("profile", choices=tuple(resolver.PROFILE_PATHS))
    parser.add_argument("--min-lines", type=int, required=True)
    parser.add_argument("--min-tokens", type=int, required=True)
    parser.add_argument("--threshold", type=float, required=True)
    return parser.parse_args()


def _footprint(duplicate: JscpdDuplicate) -> int:
    return min(
        int(duplicate["firstFile"]["end"]) - int(duplicate["firstFile"]["start"]) + 1,
        int(duplicate["secondFile"]["end"]) - int(duplicate["secondFile"]["start"]) + 1,
    )


def _run(args: argparse.Namespace) -> int:
    """One whole-repo jscpd invocation, scored as a gate."""
    resolver._validate_scan_config(resolver.CONFIG_PATH)
    _require_configured_floor(args)
    floor = (args.min_lines, args.min_tokens)
    report = resolver._whole_tree_scan(floor, args)
    statistics = report["statistics"]["total"]
    sources = int(statistics["sources"])
    if sources < MIN_SOURCES[args.profile]:
        print(f"jscpd {args.profile}: analyzed only {sources} sources", file=sys.stderr)
        return 1

    duplicated_lines = int(statistics["duplicatedLines"])
    total_lines = int(statistics.get("lines", 0))
    percentage = 100 * duplicated_lines / total_lines if total_lines else 0.0
    duplicates = report["duplicates"]
    for duplicate in duplicates:
        print(
            f"jscpd {args.profile}: [{resolver._tree_of(duplicate, args.profile)}] "
            f"duplicate: {resolver._pair_for(duplicate, args.profile)}",
            file=sys.stderr,
        )

    # A duplicate at the floor fails the gate: the hook runs at --threshold 0,
    # so every duplicate is a finding.
    if duplicates:
        return 1

    if percentage > args.threshold:
        print(
            f"jscpd {args.profile}: {percentage:.6f}% duplicated exceeds {args.threshold:.6f}%",
            file=sys.stderr,
        )
        return 1

    print(
        f"jscpd {args.profile}: 0 pairs at {floor[0]} lines/{floor[1]} tokens, "
        f"{duplicated_lines} duplicated lines of {total_lines} ({sources} sources)"
    )
    return 0


def main() -> int:
    try:
        return _run(_parse_args())
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"jscpd gate failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
