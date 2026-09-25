"""Run a duplication profile and reject every pair not declared in the checked-in expectations."""

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
PROFILE_PATHS = {"all": (".",)}
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
    parser.add_argument("--min-lines", type=int, required=True)
    parser.add_argument("--min-tokens", type=int, required=True)
    parser.add_argument("--threshold", type=float, required=True)
    parser.add_argument("--fail-on-empty", action="store_true", required=True)
    return parser.parse_args()


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
    """Fail closed unless `.jscpd.json` is exactly the sanctioned ignore list.

    Checked before jscpd runs, not after: a scan taken under an unvalidated
    config has already decided what the gate cannot see, so there is nothing
    left to check afterwards.
    """
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or not isinstance(document.get("ignore"), list):
        raise ValueError(f"{path.name} must be an object with an ignore list")
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
    for pair in document["pairs"]:
        _validate_expectation(pair)
        pair_id = pair["id"]
        if pair_id in seen_ids:
            raise ValueError(f"duplicate jscpd expectation id: {pair_id!r}")
        seen_ids.add(pair_id)
    return document["pairs"]


def _location_pair(entries: list[LocationKey]) -> LocationPair:
    first, second = sorted(entries)
    return (first, second)


def _location(container: dict[str, Any], side: str, path_key: str, default_dir: str | None = None) -> LocationKey:
    path = str(container[side][path_key])
    if default_dir is not None and "/" not in path:
        path = f"{default_dir}/{path}"
    return (path, int(container[side]["start"]), int(container[side]["end"]))


def _expected_locations(pair: dict[str, Any]) -> LocationPair:
    return _location_pair([_location(pair, side, "path") for side in ("first", "second")])


def _pair_id(duplicate: dict[str, Any]) -> LocationPair:
    """Where jscpd says a clone is.

    Its `name` is relative to the scan root, so a name with no directory
    component is a file at the root of the tree. Those are attributed to `app/`,
    the strictest tree, so a stray root-level file is gated as production rather
    than passing through unclassified.
    """
    return _location_pair([_location(duplicate, side, "name", "app") for side in ("firstFile", "secondFile")])


def _short(fragment_hash: str) -> str:
    return fragment_hash[:12]


def _run(args: argparse.Namespace) -> int:
    _validate_scan_config(CONFIG_PATH)
    with tempfile.TemporaryDirectory(prefix="jscpd-gate-") as raw_output:
        output = Path(raw_output)
        command = [
            str(SERVICE_ROOT / ".venv" / "bin" / "jscpd"),
            "--config",
            str(CONFIG_PATH),
            "-f",
            "python",
            "--min-lines",
            str(args.min_lines),
            "--min-tokens",
            str(args.min_tokens),
            "--threshold",
            str(args.threshold),
            "--fail-on-empty",
            "--reporters",
            "json",
            "--output",
            str(output),
            *PROFILE_PATHS[args.profile],
        ]
        completed = subprocess.run(command, cwd=SERVICE_ROOT, capture_output=True, text=True, check=False)
        report_path = output / "jscpd-report.json"
        if completed.returncode not in (0, 1) or not report_path.is_file():
            print(completed.stdout + completed.stderr, file=sys.stderr)
            return completed.returncode or 1
        report = json.loads(report_path.read_text(encoding="utf-8"))
        statistics = report["statistics"]["total"]
        if int(statistics["sources"]) < MIN_SOURCES[args.profile]:
            print(
                f"jscpd {args.profile}: analyzed only {statistics['sources']} sources",
                file=sys.stderr,
            )
            return 1
        # A sanctioned clone is identified by its CONTENT. Identity used to be
        # (path, start, end), and `missing` was tested before `mismatched`, so
        # inserting one comment line above a sanctioned clone reported it as a
        # disappearance and the fix-forward was to copy the coordinates out of
        # the error message into the expectation -- an expectation whose stated
        # location nothing ever checked. Content identity makes a moved clone
        # the same clone, which turns that into drift: a failure that names the
        # expectation and states where the content is now, so correcting it is a
        # reviewed edit rather than a copy of whatever the tool printed.
        actual_locations: dict[str, set[LocationPair]] = {}
        actual_duplicate: dict[str, dict[str, Any]] = {}
        for duplicate in report["duplicates"]:
            fragment_hash = _fragment_hash(duplicate)
            actual_locations.setdefault(fragment_hash, set()).add(_pair_id(duplicate))
            actual_duplicate.setdefault(fragment_hash, duplicate)

        expected: dict[str, dict[str, Any]] = {}
        for pair in _expected_pairs():
            fragment_hash = str(pair["fragment_sha256"])
            if fragment_hash in expected:
                raise ValueError(
                    f"two jscpd expectations ({expected[fragment_hash]['id']!r} and {pair['id']!r}) "
                    f"claim the same fragment { _short(fragment_hash) }"
                )
            expected[fragment_hash] = pair

        unsanctioned = sorted(set(actual_locations) - set(expected))
        missing = sorted(set(expected) - set(actual_locations))
        drifted: list[tuple[dict[str, Any], tuple[LocationPair, ...]]] = []
        undeclared: list[tuple[dict[str, Any], list[LocationPair]]] = []
        for fragment_hash, pair in expected.items():
            found = actual_locations.get(fragment_hash)
            if not found:
                continue
            found_keys = {key for location_pair in found for key in location_pair}
            declared = _expected_locations(pair)
            if not set(declared) <= found_keys:
                drifted.append((pair, tuple(sorted(found))))
                continue
            extra = sorted(found_keys - set(declared))
            if extra:
                undeclared.append((pair, extra))

        if unsanctioned or missing or drifted or undeclared:
            for fragment_hash in unsanctioned:
                for location in sorted(actual_locations[fragment_hash]):
                    print(
                        f"jscpd {args.profile}: unsanctioned clone: {location} "
                        f"sha256={_short(fragment_hash)}",
                        file=sys.stderr,
                    )
            for fragment_hash in missing:
                print(
                    f"jscpd {args.profile}: expected clone is missing: "
                    f"id={expected[fragment_hash]['id']} sha256={_short(fragment_hash)}. "
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
        percentage = float(statistics["percentage"])
        duplicated_lines = int(statistics["duplicatedLines"])
        sanctioned_lines = sum(
            min(
                int(actual_duplicate[fragment_hash]["firstFile"]["end"])
                - int(actual_duplicate[fragment_hash]["firstFile"]["start"])
                + 1,
                int(actual_duplicate[fragment_hash]["secondFile"]["end"])
                - int(actual_duplicate[fragment_hash]["secondFile"]["start"])
                + 1,
            )
            for fragment_hash in expected
        )
        unsanctioned_lines = max(0, duplicated_lines - sanctioned_lines)
        total_lines = int(statistics.get("lines", 0))
        if total_lines:
            unsanctioned_percentage = 100 * unsanctioned_lines / total_lines
        else:
            unsanctioned_percentage = percentage
        if unsanctioned_percentage > args.threshold:
            print(
                f"jscpd {args.profile}: {unsanctioned_percentage:.6f}% unsanctioned "
                f"exceeds {args.threshold:.6f}%",
                file=sys.stderr,
            )
            return 1
        sanctioned = len(expected)
        print(
            f"jscpd {args.profile}: {len(actual_duplicate)} pairs, {sanctioned} sanctioned, "
            f"{statistics['duplicatedLines']} duplicated lines ({percentage:.6f}%)"
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
