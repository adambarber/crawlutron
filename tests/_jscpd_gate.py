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
MIN_SOURCES = {"app": 50, "tests": 100, "all": 200}
PROFILE_PATHS = {"app": ("app",), "tests": ("tests",), "all": (".",)}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("profile", choices=tuple(PROFILE_PATHS))
    parser.add_argument("--min-lines", type=int, required=True)
    parser.add_argument("--min-tokens", type=int, required=True)
    parser.add_argument("--threshold", type=float, required=True)
    parser.add_argument("--fail-on-empty", action="store_true", required=True)
    return parser.parse_args()


def _canonical_path(path: str, profile: str) -> str:
    if profile == "app" and "/" not in path:
        return f"app/{path}"
    return path


def _pair_id(duplicate: dict[str, Any], profile: str) -> tuple[tuple[str, int, int], ...]:
    locations = []
    for side in ("firstFile", "secondFile"):
        location = duplicate[side]
        locations.append(
            (
                _canonical_path(str(location["name"]), profile),
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
    profiles = pair.get("profiles")
    if (
        not isinstance(profiles, list)
        or not profiles
        or any(profile not in PROFILE_PATHS for profile in profiles)
    ):
        raise ValueError(f"jscpd expectation {pair_id!r} has invalid profiles")
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


def _expected_pairs(profile: str) -> list[dict[str, Any]]:
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
    return [pair for pair in document["pairs"] if profile in pair["profiles"]]


def _expected_id(pair: dict[str, Any], profile: str) -> tuple[tuple[str, int, int], ...]:
    locations = []
    for side in ("first", "second"):
        location = pair[side]
        locations.append(
            (
                _canonical_path(str(location["path"]), profile),
                int(location["start"]),
                int(location["end"]),
            )
        )
    return tuple(sorted(locations))


def _run(args: argparse.Namespace) -> int:
    with tempfile.TemporaryDirectory(prefix="jscpd-gate-") as raw_output:
        output = Path(raw_output)
        command = [
            "jscpd",
            "--config",
            str(SERVICE_ROOT / ".jscpd.json"),
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
        actual_fragments: dict[tuple[tuple[str, int, int], ...], str] = {}
        for duplicate in report["duplicates"]:
            pair_id = _pair_id(duplicate, args.profile)
            fragment_hash = _fragment_hash(duplicate)
            if pair_id in actual_fragments and actual_fragments[pair_id] != fragment_hash:
                raise ValueError(f"jscpd reported conflicting fragments for {pair_id}")
            actual_fragments[pair_id] = fragment_hash
        actual = set(actual_fragments)
        expected = {
            _expected_id(pair, args.profile): pair
            for pair in _expected_pairs(args.profile)
        }
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
        if args.profile == "app" and percentage > args.threshold:
            print(
                f"jscpd {args.profile}: {percentage:.6f}% exceeds {args.threshold:.6f}%",
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
