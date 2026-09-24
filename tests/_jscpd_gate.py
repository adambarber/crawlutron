"""Run a duplication profile and reject every pair not declared in the checked-in expectations."""

from __future__ import annotations

import argparse
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


def _expected_pairs(profile: str) -> list[dict[str, Any]]:
    document = json.loads(EXPECTATIONS_PATH.read_text(encoding="utf-8"))
    if document.get("version") != 1 or not isinstance(document.get("pairs"), list):
        raise ValueError("jscpd-expectations.json must have version 1 and a pairs list")
    return [pair for pair in document["pairs"] if profile in pair.get("profiles", [])]


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
        actual = {_pair_id(pair, args.profile) for pair in report["duplicates"]}
        expected = {
            _expected_id(pair, args.profile): pair
            for pair in _expected_pairs(args.profile)
        }
        missing = sorted(expected.keys() - actual)
        if missing:
            for pair_id in missing:
                print(f"jscpd {args.profile}: expected clone is missing: {pair_id}", file=sys.stderr)
            return 1
        unexpected = actual - expected.keys()
        if args.profile in {"tests", "all"} and unexpected:
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
