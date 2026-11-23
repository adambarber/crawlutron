"""Validate the complete staged roster before any PyPI upload is attempted."""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from scripts.release._common import _require_env, _run
from utils.paths import REPO_ROOT


def may_publish(validation: Mapping[str, bool], roster: Sequence[str]) -> bool:
    """Publish only when every roster selector was validated and none failed."""
    return set(validation) == set(roster) and all(validation.values())


def _version_matches(dist: Path, selector: str, version: str) -> bool:
    staged = dist / f"dist-{selector}"
    wheels = sorted(staged.glob("*.whl"))
    return bool(wheels) and all(version in wheel.name for wheel in wheels)


def main() -> int:
    entries = json.loads(_require_env("PACKAGES"))
    dist = REPO_ROOT / "dist"
    validation: dict[str, bool] = {}
    for entry in entries:
        selector, version = str(entry["selector"]), str(entry["version"])
        valid = _version_matches(dist, selector, version)
        print(f"{selector}: version-equals-tag={'ok' if valid else 'FAIL'}")
        validation[selector] = valid

    wheels = sorted(dist.glob("dist-*/*.whl"))
    if wheels:
        result = _run(
            ["uvx", "twine", "check", "--strict", *[str(w.relative_to(REPO_ROOT)) for w in wheels]],
            capture=True,
        )
        if result.returncode != 0:
            print(f"twine check failed: {result.stderr}", file=sys.stderr)
            validation.clear()

    if not may_publish(validation, [str(entry["selector"]) for entry in entries]):
        print("roster preflight failed: no upload is permitted", file=sys.stderr)
        return 6
    print(f"Validated {len(entries)} distribution(s); publishing may proceed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
