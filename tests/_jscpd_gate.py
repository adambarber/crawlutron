"""A duplication profile that rejects every pair not declared in the expectations baseline.

The expectations file is a DECLARED BASELINE of known, reviewed duplication,
not a record of past approvals. Each pair carries a reason and a fragment hash
that prove the list and the code agree at the time of the assertion — a hash
proves the list and the code agree, not that a human approved the duplication.
The baseline changes through a reviewed commit; GitHub rulesets return 403 on
this account, so the trust boundary is this gate plus a CODEOWNERS entry
covering `jscpd-expectations.json`.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

SERVICE_ROOT = Path(__file__).resolve().parents[1]
# A clone's location: repo-relative path and the line range jscpd reported.
LocationKey = tuple[str, int, int]
# Both sides of a clone, sorted so the pair has one representation.
LocationPair = tuple[LocationKey, LocationKey]
EXPECTATIONS_PATH = SERVICE_ROOT / "jscpd-expectations.json"
CONFIG_PATH = SERVICE_ROOT / ".jscpd.json"
MIN_SOURCES = {"all": 200}
# Which roots each floor applies to, and the trees they are allowed to be silent
# about. Every root is scanned by its own invocation with exactly one root,
# because jscpd reports `name` relative to the scan root: an invocation given
# `app/ tests/ scripts/` returns `crawl/x.py` for both `app/crawl/x.py` and
# `tests/crawl/x.py`, and a clone spanning two trees is reported against a path
# that names neither. One root per invocation is what lets this gate's own
# report say which tree a pair is in, and it is why PROFILE_PATHS lists the
# roots rather than ".".
TREE_ROOTS = {
    "production": ("app",),
    "test": ("tests", "scripts"),
}
PROFILE_PATHS = {"all": tuple(root for roots in TREE_ROOTS.values() for root in roots)}
# `.jscpd.json` decides which files this gate never sees, so it is enforcement
# configuration, and a config nothing validates is a config that can be edited
# into a no-op: one added glob is enough to hide a clone, and `ignore` was read
# from disk by the subprocess below without ever being checked. These are the
# whole sanctioned set -- caches, vendored trees, the scratch directory, and
# schema migrations -- and the gate holds them rather than trusting the file, so
# widening the scan surface is a deliberate edit to this list in a reviewed
# commit instead of a silent edit to a JSON file.
#
# The migrations subtree is excluded because parallel CREATE TABLE scaffolding
# across two migration files is the correct shape, not duplication: migrations
# are replayed by name and each one is a standalone record of one schema step.
# It is a subtree glob so a migration added tomorrow is covered without anyone
# editing this list, which is the only way an exclusion stays true.
SANCTIONED_IGNORE_GLOBS = (
    "**/__pycache__/**",
    "**/.pytest_cache/**",
    "**/tmp/**",
    "**/node_modules/**",
    "**/.git/**",
    "**/fixtures/**",
    "**/app/crawl/migrations/**",
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("profile", choices=tuple(PROFILE_PATHS))
    # The looser floor, for the test and support trees. These flag names and
    # their values are unchanged; the tighter production floor is the pair
    # below them.
    parser.add_argument("--min-lines", type=int, required=True)
    parser.add_argument("--min-tokens", type=int, required=True)
    parser.add_argument("--app-min-lines", type=int, required=True)
    parser.add_argument("--app-min-tokens", type=int, required=True)
    parser.add_argument("--threshold", type=float, required=True)
    parser.add_argument("--fail-on-empty", action="store_true", required=True)
    return parser.parse_args()


def _floors(args: argparse.Namespace) -> dict[str, tuple[int, int]]:
    """The floor each tree is scored at, as (min_lines, min_tokens).

    Separate numbers because one flat floor cannot serve both trees. At 8 lines
    and 40 tokens this whole service reported exactly one pair, and both sides
    of it were in `app/`: production duplication was invisible behind a floor
    picked for test scaffolding. At 5 and 25 the same tree reports 169 pairs,
    46 of them production. Neither end is the right answer -- the flat number
    hid the tree that matters -- so each tree is scored on its own evidence and
    both ratchet towards zero.
    """
    return {
        "production": (args.app_min_lines, args.app_min_tokens),
        "test": (args.min_lines, args.min_tokens),
    }


def _tree_of(path: str) -> str:
    """Which tree a reported path belongs to.

    Derived from the path, not declared by the expectation, so a file cannot
    choose the floor it is judged by. Anything outside the known roots is
    production: the tighter floor, so an unrecognised file cannot slip through
    on the looser one.
    """
    for tree, roots in TREE_ROOTS.items():
        if any(path == root or path.startswith(root + "/") for root in roots):
            return tree
    return "production"


def _reject_source_masking(ignore: list[str]) -> None:
    """A glob that hides source is never a sanctioned exception.

    Checked independently of the sanctioned list so that widening the list is
    not itself a way to hide a clone: the rule is that this gate's ignore list
    may skip caches and vendored trees, and nothing else.
    """
    for glob in ignore:
        if glob.rstrip("/").endswith(".py") or "*.py" in glob:
            raise ValueError(f"jscpd ignore glob would hide Python source: {glob!r}")
        if not glob.strip("*./"):
            raise ValueError(f"jscpd ignore glob would hide the whole tree: {glob!r}")


def _validate_scan_config(path: Path) -> None:
    """Fail closed unless `.jscpd.json` contains only the sanctioned `ignore` key.

    Checked before jscpd runs, not after: a scan taken under an unvalidated
    config has already decided what the gate cannot see, so there is nothing
    left to check afterwards. Only `ignore` is allowed; extra keys like
    `maxLines`, `skipLocal`, `minLines`, `minTokens` pass straight through
    to jscpd via `--config` and are live. Measured: `maxLines: 500` shrinks
    production coverage from 94 sources/21506 lines to 83/13560 (37% of
    production unscanned) while the gate still exits 0 because the expectations
    file is satisfied by the reduced pair set.
    """
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"{path.name} must be an object")
    extra = set(document) - {"ignore"}
    if extra:
        raise ValueError(f"{path.name} contains unknown keys: {sorted(extra)}")
    if not isinstance(document.get("ignore"), list):
        raise ValueError(f"{path.name} must have an ignore list")
    ignore = document["ignore"]
    if not all(isinstance(glob, str) and glob for glob in ignore):
        raise ValueError(f"{path.name} ignore entries must be non-empty strings")
    _reject_source_masking(ignore)
    if tuple(ignore) != SANCTIONED_IGNORE_GLOBS:
        added = sorted(set(ignore) - set(SANCTIONED_IGNORE_GLOBS))
        removed = sorted(set(SANCTIONED_IGNORE_GLOBS) - set(ignore))
        raise ValueError(
            f"{path.name} ignore list is not the sanctioned set "
            f"(added={added}, removed={removed}); the sanctioned globs live in "
            f"_jscpd_gate.SANCTIONED_IGNORE_GLOBS"
        )


def _fragment_hash(duplicate: dict[str, Any]) -> str:
    fragment = duplicate.get("fragment")
    if not isinstance(fragment, str):
        raise ValueError("jscpd report is missing a duplicate fragment")
    return hashlib.sha256(fragment.encode()).hexdigest()


def _test_names(path: Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError as error:
        raise ValueError(f"guard file cannot be parsed: {path}") from error

    names: set[str] = set()

    def collect(body: list[ast.stmt], prefix: str = "") -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                names.add(f"{prefix}{node.name}")
            elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                collect(node.body, f"{prefix}{node.name}::")

    collect(tree.body)
    return names


def _validate_guard(guard: Any) -> None:
    if not isinstance(guard, str) or not guard.strip():
        raise ValueError("jscpd expectation guard must name a test")
    parts = guard.split("::")
    if len(parts) < 2 or not parts[0] or not parts[-1]:
        raise ValueError(f"jscpd expectation guard has an invalid node id: {guard!r}")
    relative = Path(parts[0])
    if relative.is_absolute() or relative.parts[:1] != ("tests",) or ".." in relative.parts:
        raise ValueError(f"jscpd expectation guard must point inside tests/: {guard!r}")
    path = SERVICE_ROOT / relative
    if not path.is_file() or path.suffix != ".py":
        raise ValueError(f"jscpd expectation guard file does not exist: {guard!r}")
    node = "::".join(parts[1:])
    if node not in _test_names(path):
        raise ValueError(f"jscpd expectation guard test does not exist: {guard!r}")


def _validate_expectation(pair: Any) -> None:
    if not isinstance(pair, dict):
        raise ValueError("each jscpd expectation must be an object")
    pair_id = pair.get("id")
    if not isinstance(pair_id, str) or not pair_id.strip():
        raise ValueError("each jscpd expectation needs a non-empty id")
    if "profiles" in pair:
        raise ValueError(f"jscpd expectation {pair_id!r} uses the legacy profiles shape")
    reason = pair.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError(f"jscpd expectation {pair_id!r} needs a non-empty reason")
    _validate_guard(pair.get("guard"))
    fragment_hash = pair.get("fragment_sha256")
    if not isinstance(fragment_hash, str) or len(fragment_hash) != 64:
        raise ValueError(f"jscpd expectation {pair_id!r} needs a fragment_sha256")
    try:
        int(fragment_hash, 16)
    except ValueError as error:
        raise ValueError(f"jscpd expectation {pair_id!r} has an invalid fragment_sha256") from error
    for side in ("first", "second"):
        location = pair.get(side)
        if not isinstance(location, dict) or not isinstance(location.get("path"), str):
            raise ValueError(f"jscpd expectation {pair_id!r} has an invalid {side} location")
        path = Path(location["path"])
        if path.is_absolute() or ".." in path.parts or not location["path"]:
            raise ValueError(f"jscpd expectation {pair_id!r} has an unsafe {side} path")
        if not all(isinstance(location.get(field), int) and not isinstance(location.get(field), bool) for field in ("start", "end")):
            raise ValueError(f"jscpd expectation {pair_id!r} has invalid {side} bounds")
        if location["start"] < 1 or location["end"] < location["start"]:
            raise ValueError(f"jscpd expectation {pair_id!r} has invalid {side} bounds")


def _expected_pairs() -> list[dict[str, Any]]:
    document = json.loads(EXPECTATIONS_PATH.read_text(encoding="utf-8"))
    if document.get("version") != 1 or not isinstance(document.get("pairs"), list):
        raise ValueError("jscpd-expectations.json must have version 1 and a pairs list")
    seen_ids: set[str] = set()
    seen_locations: dict[LocationPair, str] = {}
    for pair in document["pairs"]:
        _validate_expectation(pair)
        pair_id = pair["id"]
        if pair_id in seen_ids:
            raise ValueError(f"duplicate jscpd expectation id: {pair_id!r}")
        seen_ids.add(pair_id)
        # Two expectations on the same lines is not a duplicate the gate can
        # resolve: at most one can ever be satisfied, and the loser is reported
        # as a missing clone, which sends the reader after the duplication
        # instead of after this file. Rejecting it names the actual mistake.
        locations = _expected_locations(pair)
        if locations in seen_locations:
            raise ValueError(
                f"jscpd expectations {seen_locations[locations]!r} and {pair_id!r} "
                f"declare the same location: {list(locations)}"
            )
        seen_locations[locations] = pair_id
    return document["pairs"]


def _location_pair(entries: list[LocationKey]) -> LocationPair:
    first, second = sorted(entries)
    return (first, second)


def _location(container: dict[str, Any], side: str, path_key: str) -> LocationKey:
    return (str(container[side][path_key]), int(container[side]["start"]), int(container[side]["end"]))


def _expected_locations(pair: dict[str, Any]) -> LocationPair:
    return _location_pair([_location(pair, side, "path") for side in ("first", "second")])


def _pair_id(duplicate: dict[str, Any], root: str) -> LocationPair:
    """Where jscpd says a clone is, in repository-relative terms.

    jscpd's `name` is relative to the scan root, so the root is put back on.
    Without it, `models.py` scanned from `app/` and `models.py` scanned from
    `tests/` are the same string, and a single whole-tree scan cannot say which
    tree a pair belongs to.
    """
    return _location_pair([_scoped(root, _location(duplicate, side, "name")) for side in ("firstFile", "secondFile")])


def _scoped(root: str, key: LocationKey) -> LocationKey:
    path = key[0]
    scoped = path if root == "." or path == root or path.startswith(root + "/") else f"{root}/{path}"
    return (scoped, key[1], key[2])


def _short(fragment_hash: str) -> str:
    return fragment_hash[:12]


def _scan(root: str, floor: tuple[int, int], args: argparse.Namespace) -> dict[str, Any]:
    """One jscpd invocation over one root at one floor."""
    with tempfile.TemporaryDirectory(prefix="jscpd-gate-") as raw_output:
        output = Path(raw_output)
        command = [
            str(SERVICE_ROOT / ".venv" / "bin" / "jscpd"),
            "--config",
            str(CONFIG_PATH),
            "-f",
            "python",
            "--min-lines",
            str(floor[0]),
            "--min-tokens",
            str(floor[1]),
            "--threshold",
            str(args.threshold),
            "--fail-on-empty",
            "--reporters",
            "json",
            "--output",
            str(output),
            root,
        ]
        completed = subprocess.run(command, cwd=SERVICE_ROOT, capture_output=True, text=True, check=False)
        report_path = output / "jscpd-report.json"
        if completed.returncode not in (0, 1) or not report_path.is_file():
            raise OSError(f"jscpd failed on {root}: {completed.stdout}{completed.stderr}".strip())
        return json.loads(report_path.read_text(encoding="utf-8"))


def _footprint(duplicate: dict[str, Any]) -> int:
    return min(
        int(duplicate["firstFile"]["end"]) - int(duplicate["firstFile"]["start"]) + 1,
        int(duplicate["secondFile"]["end"]) - int(duplicate["secondFile"]["start"]) + 1,
    )


def _run(args: argparse.Namespace) -> int:
    _validate_scan_config(CONFIG_PATH)
    floors = _floors(args)
    # Keyed by (tree, content). The tree is part of the identity because the
    # same fragment in `app/` and in `tests/` is two separate findings at two
    # different floors, and a single whole-tree scan keyed on content alone
    # cannot tell them apart.
    observed: dict[tuple[str, str], set[LocationPair]] = {}
    spans: dict[tuple[str, str], int] = {}
    totals: dict[str, dict[str, int]] = {}
    sources = 0
    for tree, roots in TREE_ROOTS.items():
        for root in roots:
            report = _scan(root, floors[tree], args)
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
                fragment_hash = _fragment_hash(duplicate)
                key = (tree, fragment_hash)
                observed.setdefault(key, set()).add(_pair_id(duplicate, root))
                spans.setdefault(key, _footprint(duplicate))
                tree_totals["pairs"] += 1

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
    for pair in _expected_pairs():
        fragment_hash = str(pair["fragment_sha256"])
        key = (_tree_of(_expected_locations(pair)[0][0]), fragment_hash)
        if key in expected:
            raise ValueError(
                f"two jscpd expectations ({expected[key]['id']!r} and {pair['id']!r}) "
                f"claim the same fragment { _short(fragment_hash) }"
            )
        expected[key] = pair

    unsanctioned = sorted(set(observed) - set(expected))
    missing = sorted(set(expected) - set(observed))
    drifted: list[tuple[dict[str, Any], tuple[LocationPair, ...]]] = []
    undeclared: list[tuple[dict[str, Any], list[LocationPair]]] = []
    for key, pair in expected.items():
        found = observed.get(key)
        if not found:
            continue
        found_keys = {location for location_pair in found for location in location_pair}
        declared = _expected_locations(pair)
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
                    f"sha256={_short(key[1])}",
                    file=sys.stderr,
                )
        for key in missing:
            print(
                f"jscpd {args.profile}: [{key[0]}] expected clone is missing: "
                f"id={expected[key]['id']} sha256={_short(key[1])}. "
                f"Either the duplication was fixed, in which case delete the expectation on "
                f"purpose, or its content changed, in which case it is an unsanctioned clone above.",
                file=sys.stderr,
            )
        for pair, found in drifted:
            print(
                f"jscpd {args.profile}: expected clone drifted: id={pair['id']} declares "
                f"{_expected_locations(pair)} but its content is now at {list(found)}. "
                f"Update the expectation to match, in this commit, on purpose.",
                file=sys.stderr,
            )
        for pair, extra in undeclared:
            print(
                f"jscpd {args.profile}: sanctioned clone also appears undeclared: "
                f"id={pair['id']} at {extra}",
                file=sys.stderr,
            )
        return 1

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
            f"{floors[tree][0]} lines/{floors[tree][1]} tokens, "
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
