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
EXPECTATIONS_PATH = SERVICE_ROOT / "jscpd-expectations.json"
CONFIG_PATH = SERVICE_ROOT / ".jscpd.json"
MIN_SOURCES = {"all": 200}
PROFILE_PATHS = {"all": (".",)}
# `.jscpd.json` decides which files this gate never sees, so it is enforcement
# configuration, and a config nothing validates is a config that can be edited
# into a no-op: one added glob is enough to hide a clone, and `ignore` was read
# from disk by the subprocess below without ever being checked. These six globs
# are the whole sanctioned set -- caches, vendored trees, and the scratch
# directory -- and the gate holds them rather than trusting the file, so
# widening the scan surface is a deliberate edit to this list in a reviewed
# commit instead of a silent edit to a JSON file.
SANCTIONED_IGNORE_GLOBS = (
    "**/__pycache__/**",
    "**/.pytest_cache/**",
    "**/tmp/**",
    "**/node_modules/**",
    "**/.git/**",
    "**/fixtures/**",
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


def _pair_id(duplicate: dict[str, Any]) -> tuple[tuple[str, int, int], ...]:
    locations = []
    for side in ("firstFile", "secondFile"):
        location = duplicate[side]
        path = str(location["name"])
        locations.append(
            (
                path if "/" in path else f"app/{path}",
                int(location["start"]),
                int(location["end"]),
            )
        )
    return tuple(sorted(locations))


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


def _expected_id(pair: dict[str, Any]) -> tuple[tuple[str, int, int], ...]:
    locations = []
    for side in ("first", "second"):
        location = pair[side]
        locations.append(
            (
                str(location["path"]),
                int(location["start"]),
                int(location["end"]),
            )
        )
    return tuple(sorted(locations))


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
        actual_duplicates: dict[tuple[tuple[str, int, int], ...], dict[str, Any]] = {}
        actual_fragments: dict[tuple[tuple[str, int, int], ...], str] = {}
        for duplicate in report["duplicates"]:
            pair_id = _pair_id(duplicate)
            fragment_hash = _fragment_hash(duplicate)
            if pair_id in actual_fragments and actual_fragments[pair_id] != fragment_hash:
                raise ValueError(f"jscpd reported conflicting fragments for {pair_id}")
            actual_duplicates[pair_id] = duplicate
            actual_fragments[pair_id] = fragment_hash
        actual = set(actual_fragments)
        expected = {_expected_id(pair): pair for pair in _expected_pairs()}
        missing = sorted(expected.keys() - actual)
        if missing:
            for pair_id in missing:
                print(f"jscpd {args.profile}: expected clone is missing: {pair_id}", file=sys.stderr)
            return 1
        mismatched = [
            pair_id
            for pair_id, pair in expected.items()
            if actual_fragments[pair_id] != pair["fragment_sha256"]
        ]
        if mismatched:
            for pair_id in sorted(mismatched):
                print(f"jscpd {args.profile}: expected clone fragment changed: {pair_id}", file=sys.stderr)
            return 1
        unexpected = actual - expected.keys()
        if unexpected:
            for pair_id in sorted(unexpected):
                print(f"jscpd {args.profile}: unsanctioned clone: {pair_id}", file=sys.stderr)
            return 1
        percentage = float(statistics["percentage"])
        duplicated_lines = int(statistics["duplicatedLines"])
        sanctioned_lines = sum(
            min(
                int(actual_duplicates[pair_id]["firstFile"]["end"])
                - int(actual_duplicates[pair_id]["firstFile"]["start"])
                + 1,
                int(actual_duplicates[pair_id]["secondFile"]["end"])
                - int(actual_duplicates[pair_id]["secondFile"]["start"])
                + 1,
            )
            for pair_id in expected
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
            f"jscpd {args.profile}: {len(actual)} pairs, {sanctioned} sanctioned, "
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
