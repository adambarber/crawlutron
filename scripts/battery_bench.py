"""Reproducible battery timing: one command, one summary line, no guessing."""

from __future__ import annotations

import argparse
import configparser
import re
import subprocess
import sys

from utils.paths import REPO_ROOT

_WORKERS = re.compile(r"-n\s+(\S+)")
_SUMMARY = re.compile(r"\d+ passed.* in [\d:.]+")


def addopts() -> str:
    """The suite's addopts, read from pytest.ini rather than restated here."""
    config = configparser.ConfigParser()
    config.read(REPO_ROOT / "pytest.ini")
    return " ".join(config["pytest"]["addopts"].split())


def configured_workers() -> int:
    """The worker count the suite already declares, so the default cannot drift."""
    match = _WORKERS.search(addopts())
    if match is None:
        raise ValueError("pytest.ini declares no worker count")
    return int(match.group(1))


def command(workers: int | None, durations: int) -> list[str]:
    """Build one pytest invocation with the configured flags minus its worker count."""
    base = " ".join(_WORKERS.sub("", addopts()).split())
    argv = [sys.executable, "-m", "pytest", "-q", "-o", f"addopts={base}", f"--durations={durations}"]
    return argv + (["-n", str(workers)] if workers is not None else [])


def options(argv: list[str] | None) -> tuple[int | None, int]:
    """Parse the knobs; default workers come from pytest.ini, not a literal."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int)
    parser.add_argument("--serial", action="store_true")
    parser.add_argument("--durations", type=int, default=30)
    args = parser.parse_args(argv)
    if args.serial and args.workers is not None:
        raise ValueError("--serial and --workers are mutually exclusive")
    workers = None if args.serial else (configured_workers() if args.workers is None else args.workers)
    return workers, args.durations


def main() -> int:
    workers, durations = options(sys.argv[1:])
    result = subprocess.run(
        command(workers, durations),
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    lines = [line.strip() for line in result.stdout.splitlines() if _SUMMARY.search(line)]
    print(lines[-1] if lines else (result.stdout[-400:] + result.stderr[-400:]))
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
