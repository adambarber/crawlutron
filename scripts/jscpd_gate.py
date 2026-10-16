"""A duplication profile that rejects every pair not declared in the expectations baseline.

The baseline is DECLARED, not a record of approvals -- each pair carries a reason + fragment
hash; the trust boundary is this gate plus a CODEOWNERS entry over ``jscpd-expectations.json``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any

import yaml

from scripts import _jscpd_pair_resolution as resolver

# Re-exports: the contract surface the gate's own suite reads off this module.
CONFIG_PATH = resolver.CONFIG_PATH
EXPECTATIONS_PATH = resolver.EXPECTATIONS_PATH
PRECOMMIT_CONFIG_PATH = resolver.PRECOMMIT_CONFIG_PATH
PROFILE_PATHS = resolver.PROFILE_PATHS
SANCTIONED_IGNORE_GLOBS = resolver.SANCTIONED_IGNORE_GLOBS
SERVICE_ROOT = resolver.SERVICE_ROOT
TREE_ROOTS = resolver.TREE_ROOTS


def configured_floor() -> tuple[int, int, float]:
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
            f"scanning at ({args.min_lines} lines / {args.min_tokens} tokens / {threshold}%) is "
            f"laxer than the enforced floor ({min_lines} / {min_tokens} / {threshold}%) in "
            f"{resolver.PRECOMMIT_CONFIG_PATH.name}: a clean result would only mean the clone is "
            f"below the number asked for. Scan stricter to measure a candidate floor."
        )


__all__ = [
    "CONFIG_PATH",
    "EXPECTATIONS_PATH",
    "PRECOMMIT_CONFIG_PATH",
    "PROFILE_PATHS",
    "SANCTIONED_IGNORE_GLOBS",
    "SERVICE_ROOT",
    "TREE_ROOTS",
]

MIN_SOURCES = {"all": 200}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("profile", choices=tuple(resolver.PROFILE_PATHS))
    # One floor, applied to every root. The split it replaced (app 7/35, tests
    # 8/40) existed because a single loose floor hid production duplication
    # behind a number picked for test scaffolding. At 4/39 both trees report
    # zero, so there is nothing left to hide and a second number is a second
    # thing to ratchet.
    parser.add_argument("--min-lines", type=int, required=True)
    parser.add_argument("--min-tokens", type=int, required=True)
    parser.add_argument("--threshold", type=float, required=True)
    parser.add_argument("--fail-on-empty", action="store_true", required=True)
    return parser.parse_args()


_IMPORT_LINE = re.compile(r"^(?:import|from)\s")


def _is_dependency_declaration(fragment: str) -> bool:
    """True when a fragment is nothing but import statements -- byte-identical text Python
    cannot hold for the modules (so it cannot drift OR be a defect), but only when EVERY logical
    line is an import; it need not end in balanced parens.
    """
    logical: list[str] = []
    open_parens = 0
    for line in fragment.splitlines():
        stripped = line.strip()
        # A line inside an open paren continues the statement above it, so it is
        # not a statement of its own and must not be judged as one.
        continuation = open_parens > 0
        open_parens += line.count("(") - line.count(")")
        if not stripped or stripped.startswith("#"):
            continue
        if not continuation:
            logical.append(stripped)
    return bool(logical) and all(_IMPORT_LINE.match(line) for line in logical)


def _footprint(duplicate: dict[str, Any]) -> int:
    return min(
        int(duplicate["firstFile"]["end"]) - int(duplicate["firstFile"]["start"]) + 1,
        int(duplicate["secondFile"]["end"]) - int(duplicate["secondFile"]["start"]) + 1,
    )


def _run(args: argparse.Namespace) -> int:
    resolver._validate_scan_config(resolver.CONFIG_PATH)
    _require_configured_floor(args)
    floor = (args.min_lines, args.min_tokens)
    # Keyed by (tree, content). The tree is part of the identity because the
    # same fragment in `app/` and in `tests/` is two separate findings against
    # two separate scan roots, and jscpd reports root-relative names, so a
    # single whole-tree scan keyed on content alone cannot tell them apart.
    observed: dict[tuple[str, str], set[resolver.LocationPair]] = {}
    spans: dict[tuple[str, str], int] = {}
    totals: dict[str, dict[str, int]] = {}
    sources = 0
    # Counted, not silently dropped: a filter that hides its own hits is
    # indistinguishable from a filter that hides too much. The lines are
    # subtracted from the duplication statistic too, because jscpd counted them
    # and leaving them in would hold the percentage over the threshold for
    # fragments this pass has already judged.
    declarations = 0
    declaration_lines = 0
    for tree, roots in TREE_ROOTS.items():
        for root in roots:
            report = resolver._scan(root, floor, args)
            statistics = report["statistics"]["total"]
            found_sources = int(statistics["sources"])
            sources += found_sources
            if found_sources < 1:
                print(
                    f"jscpd {args.profile}: [{tree}] {root} analyzed {found_sources} sources; "
                    f"the scan root is wrong or the tree is empty",
                    file=sys.stderr,
                )
                return 1
            tree_totals = totals.setdefault(
                tree, {"lines": 0, "duplicatedLines": 0, "pairs": 0, "sources": 0}
            )
            tree_totals["lines"] += int(statistics.get("lines", 0))
            tree_totals["duplicatedLines"] += int(statistics["duplicatedLines"])
            tree_totals["sources"] += found_sources
            for duplicate in report["duplicates"]:
                if _is_dependency_declaration(str(duplicate.get("fragment", ""))):
                    declarations += 1
                    declaration_lines += _footprint(duplicate)
                    continue
                fragment_hash = resolver._fragment_hash(duplicate)
                key = (tree, fragment_hash)
                observed.setdefault(key, set()).add(resolver._pair_id(duplicate, root))
                spans.setdefault(key, _footprint(duplicate))
                tree_totals["pairs"] += 1
            tree_totals["duplicatedLines"] -= declaration_lines
            declaration_lines = 0

    # The cross-tree pass runs after the per-root ones and deliberately does NOT
    # fold its source count into `sources`: it reads the same files a second
    # time, so counting them twice would let the MIN_SOURCES floor be met with
    # half the sources it names.
    cross = resolver._cross_tree_scan(floor, args)
    cross_sources = int(cross["statistics"]["total"]["sources"])
    if cross_sources < 1:
        print(
            f"jscpd {args.profile}: [cross] {resolver.CROSS_TREE_ROOT} analyzed {cross_sources} sources; "
            f"the scan root is wrong or every tree is empty",
            file=sys.stderr,
        )
        return 1
    cross_totals = {"lines": 0, "duplicatedLines": 0, "pairs": 0, "sources": cross_sources}
    for duplicate in cross["duplicates"]:
        if _is_dependency_declaration(str(duplicate.get("fragment", ""))):
            declarations += 1
            continue
        # Only pairs that SPAN two roots are this pass's business. A pair inside
        # one tree was already scored above against the same floor, and reporting
        # it twice would charge one clone against two expectations.
        if not resolver._spans_trees(duplicate, args.profile):
            continue
        fragment_hash = resolver._fragment_hash(duplicate)
        key = (resolver.CROSS_TREE, fragment_hash)
        observed.setdefault(key, set()).add(resolver._cross_pair_id(duplicate, args.profile))
        spans.setdefault(key, _footprint(duplicate))
        cross_totals["pairs"] += 1
    totals[resolver.CROSS_TREE] = cross_totals

    if sources < MIN_SOURCES[args.profile]:
        print(f"jscpd {args.profile}: analyzed only {sources} sources", file=sys.stderr)
        return 1

    # A sanctioned clone is identified by its CONTENT, within its tree. Identity
    # used to be (path, start, end), and `missing` was tested before
    # `mismatched`, so inserting one comment line above a sanctioned clone
    # reported it as a disappearance and the fix-forward was to copy the
    # coordinates out of the error message into the expectation -- an
    # expectation whose stated location nothing ever checked. Content identity
    # makes a moved clone the same clone, which turns that into drift: a failure
    # that names the expectation and states where the content is now, so
    # correcting it is a reviewed edit rather than a copy of what was printed.
    expected: dict[tuple[str, str], dict[str, Any]] = {}
    for pair in resolver._expected_pairs():
        fragment_hash = str(pair["fragment_sha256"])
        key = (resolver._expected_tree(pair), fragment_hash)
        if key in expected:
            raise ValueError(
                f"two jscpd expectations ({expected[key]['id']!r} and {pair['id']!r}) "
                f"claim the same fragment {resolver._short(fragment_hash)}"
            )
        expected[key] = pair

    unsanctioned = sorted(set(observed) - set(expected))
    missing = sorted(set(expected) - set(observed))
    drifted: list[tuple[dict[str, Any], tuple[resolver.LocationPair, ...]]] = []
    undeclared: list[tuple[dict[str, Any], list[resolver.LocationKey]]] = []
    for key, pair in expected.items():
        found = observed.get(key)
        if not found:
            continue
        found_keys = {location for location_pair in found for location in location_pair}
        declared = resolver._expected_locations(pair)
        if not set(declared) <= found_keys:
            drifted.append((pair, tuple(sorted(found))))
            continue
        extra = sorted(found_keys - set(declared))
        if extra:
            undeclared.append((pair, extra))

    if unsanctioned or missing or drifted or undeclared:
        for key in unsanctioned:
            for location in sorted(observed[key]):
                print(
                    f"jscpd {args.profile}: [{key[0]}] unsanctioned clone: {location} "
                    f"sha256={resolver._short(key[1])}",
                    file=sys.stderr,
                )
        for key in missing:
            print(
                f"jscpd {args.profile}: [{key[0]}] expected clone is missing: "
                f"id={expected[key]['id']} sha256={resolver._short(key[1])}. "
                f"Either the duplication was fixed, in which case delete the expectation on "
                f"purpose, or its content changed, in which case it is an unsanctioned clone above.",
                file=sys.stderr,
            )
        for pair, drifted_at in drifted:
            print(
                f"jscpd {args.profile}: expected clone drifted: id={pair['id']} declares "
                f"{resolver._expected_locations(pair)} but its content is now at {list(drifted_at)}. "
                f"Update the expectation to match, in this commit, on purpose.",
                file=sys.stderr,
            )
        for pair, undeclared_at in undeclared:
            print(
                f"jscpd {args.profile}: sanctioned clone also appears undeclared: "
                f"id={pair['id']} at {undeclared_at}",
                file=sys.stderr,
            )
        return 1

    if declarations:
        print(
            f"jscpd {args.profile}: skipped {declarations} fragment(s) that are nothing but "
            f"import statements. Two modules importing the same names is a shared vocabulary, "
            f"not a second copy of a decision; :func:`_is_dependency_declaration` says why.",
            file=sys.stderr,
        )

    for tree, tree_totals in totals.items():
        sanctioned_lines = sum(spans[key] for key in expected if key[0] == tree)
        unsanctioned_lines = max(0, tree_totals["duplicatedLines"] - sanctioned_lines)
        total_lines = tree_totals["lines"]
        percentage = 100 * unsanctioned_lines / total_lines if total_lines else 0.0
        if percentage > args.threshold:
            print(
                f"jscpd {args.profile}: [{tree}] {percentage:.6f}% unsanctioned "
                f"exceeds {args.threshold:.6f}%",
                file=sys.stderr,
            )
            return 1
        print(
            f"jscpd {args.profile}: [{tree}] {tree_totals['pairs']} pairs at "
            f"{floor[0]} lines/{floor[1]} tokens, "
            f"{sum(1 for key in expected if key[0] == tree)} sanctioned, "
            f"{tree_totals['duplicatedLines']} duplicated lines of {total_lines} "
            f"({tree_totals['sources']} sources)"
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
