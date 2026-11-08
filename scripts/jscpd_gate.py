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

# The scan flows' shapes, named once: the tree pass accumulates per-tree totals
# and returns (sources, duplicates); the cross pass counts only spanned pairs.
TreeTotals = dict[str, int]
TotalsByTree = dict[str, TreeTotals]
ScanTotals = tuple[int, int]
CrossScanTotals = tuple[int, int]


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
    resolver._validate_scan_config(resolver.CONFIG_PATH)
    _require_configured_floor(args)
    floor = (args.min_lines, args.min_tokens)
    totals: TotalsByTree = {}
    sources, duplicated = _scan_trees(args, floor, totals)
    if sources < 0:
        return 1

    # The cross-tree pass reads the same files a second time; counting them twice
    # would let the MIN_SOURCES floor be met with half the sources it names.
    cross_sources, cross_pairs = _scan_cross_tree(args, floor)
    if cross_sources < 0:
        return 1
    duplicated += cross_pairs
    totals[resolver.CROSS_TREE] = {
        "lines": 0,
        "duplicatedLines": 0,
        "pairs": cross_pairs,
        "sources": cross_sources,
    }
    # The cross pass reads the same files a second time; its sources stay out
    # of the MIN_SOURCES check (double-counting would halve the named floor).

    if sources < MIN_SOURCES[args.profile]:
        print(f"jscpd {args.profile}: analyzed only {sources} sources", file=sys.stderr)
        return 1

    # A duplicate at the floor fails the gate: the hook runs at --threshold 0,
    # so every duplicate is a finding.
    if duplicated:
        return 1

    for tree, tree_totals in totals.items():
        failed = _report_tree(args, floor, tree, tree_totals)
        if failed:
            return 1
    return 0


def _scan_trees(args: argparse.Namespace, floor: tuple[int, int], totals: TotalsByTree) -> ScanTotals:
    """Scan every tree root at the floor; (sources, duplicates), failures -1."""
    sources = 0
    duplicated = 0
    for tree, roots in TREE_ROOTS.items():
        for root in roots:
            report = resolver._scan(root, floor, args)
            statistics = report["statistics"]["total"]
            found_sources = int(statistics["sources"])
            if found_sources < 1:
                print(
                    f"jscpd {args.profile}: [{tree}] {root} analyzed {found_sources} sources; "
                    f"the scan root is wrong or the tree is empty",
                    file=sys.stderr,
                )
                return -1, 0
            sources += found_sources
            tree_totals = totals.setdefault(
                tree, {"lines": 0, "duplicatedLines": 0, "pairs": 0, "sources": 0}
            )
            tree_totals["lines"] += int(statistics.get("lines", 0))
            tree_totals["duplicatedLines"] += int(statistics["duplicatedLines"])
            tree_totals["sources"] += found_sources
            for duplicate in report["duplicates"]:
                tree_totals["pairs"] += 1
                duplicated += 1
                print(
                    f"jscpd {args.profile}: [{tree}] duplicate: {resolver._pair_id(duplicate, root)}",
                    file=sys.stderr,
                )
    return sources, duplicated


def _scan_cross_tree(args: argparse.Namespace, floor: tuple[int, int]) -> CrossScanTotals:
    """The cross-tree pass; (sources, spanned pairs), failures (-1, 0).

    Only pairs that SPAN two roots count; reporting a pair twice charges one
    clone against two passes.
    """
    cross = resolver._cross_tree_scan(floor, args)
    cross_sources = int(cross["statistics"]["total"]["sources"])
    if cross_sources < 1:
        print(
            f"jscpd {args.profile}: [cross] {resolver.CROSS_TREE_ROOT} analyzed {cross_sources} sources; "
            f"the scan root is wrong or every tree is empty",
            file=sys.stderr,
        )
        return -1, 0
    cross_pairs = 0
    for duplicate in cross["duplicates"]:
        if not resolver._spans_trees(duplicate, args.profile):
            continue
        cross_pairs += 1
        print(
            f"jscpd {args.profile}: [cross] duplicate: {resolver._cross_pair_id(duplicate, args.profile)}",
            file=sys.stderr,
        )
    return cross_sources, cross_pairs


def _report_tree(
    args: argparse.Namespace,
    floor: tuple[int, int],
    tree: str,
    tree_totals: TreeTotals,
) -> bool:
    """One tree's percentage report; True when it exceeds the threshold."""
    total_lines = tree_totals["lines"]
    percentage = 100 * tree_totals["duplicatedLines"] / total_lines if total_lines else 0.0
    if percentage > args.threshold:
        print(
            f"jscpd {args.profile}: [{tree}] {percentage:.6f}% duplicated exceeds {args.threshold:.6f}%",
            file=sys.stderr,
        )
        return True
    print(
        f"jscpd {args.profile}: [{tree}] {tree_totals['pairs']} pairs at "
        f"{floor[0]} lines/{floor[1]} tokens, "
        f"{tree_totals['duplicatedLines']} duplicated lines of {total_lines} "
        f"({tree_totals['sources']} sources)"
    )
    return False


def main() -> int:
    try:
        return _run(_parse_args())
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"jscpd gate failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
