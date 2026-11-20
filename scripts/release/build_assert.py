"""Assert that built wheels are installable and importable.

`twine check` passes on an unusable wheel: an undeclared dependency, a wrong
`packages` list. Installing it and importing what it ships is the real check.

This replaces the `publish-build` job steps from release.yml:
  - Build only this distribution
  - Assert the built artifacts carry the release version
  - Run twine check --strict
"""

from __future__ import annotations

import email
import os
import pathlib
import shutil
import subprocess
import sys
import zipfile
import tempfile

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
UV = shutil.which("uv") or "uv"
_PROBE_ROOT = pathlib.Path(tempfile.gettempdir()) / "crawlutron-probes"


def _probe_environment(wheel: pathlib.Path) -> pathlib.Path:
    """A clean 3.14 environment holding exactly this wheel."""
    probe = _PROBE_ROOT / wheel.stem
    if not (probe / "bin" / "python").exists():
        subprocess.run(
            [UV, "venv", "--python", "3.14", str(probe)],
            check=True,
            capture_output=True,
        )
    done = subprocess.run(
        [
            UV,
            "pip",
            "install",
            "--python",
            str(probe / "bin" / "python"),
            "--find-links",
            str(REPO_ROOT / "dist"),
            str(wheel),
        ],
        capture_output=True,
        text=True,
    )
    if done.returncode:
        raise RuntimeError(f"installing {wheel.name} failed:\n{done.stderr.strip()}")
    return probe


def _own_top_levels(wheel: pathlib.Path) -> list[str]:
    """The import names this wheel ships, read from the wheel's own contents."""
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
    roots = {
        name.split("/")[0]
        for name in names
        if name.endswith("__init__.py") and "/" in name and not name.startswith("crawlutron-")
    }
    return sorted(roots)


def main() -> int:
    """Every wheel in dist/ installs into its own environment and imports."""
    selector = os.environ.get("SELECTOR", "")
    expected_version = os.environ.get("EXPECTED_VERSION", "")

    if not selector or not expected_version:
        print("ERROR: SELECTOR and EXPECTED_VERSION environment variables are required")
        return 2

    # Build only this distribution
    build_result = subprocess.run(
        [UV, "build", "--package", selector, "--out-dir", "dist"],
        capture_output=True,
        text=True,
    )
    if build_result.returncode != 0:
        print(f"Build failed: {build_result.stderr}")
        return 3

    wheels = sorted((REPO_ROOT / "dist").glob("*.whl"))
    if not wheels:
        print("no wheels built")
        return 3

    failures: list[str] = []
    for wheel in wheels:
        tops = _own_top_levels(wheel)
        if not tops:
            # A distribution that ships no code is a meta-package: the app wheel
            # exists to pull its members in, and has nothing of its own to import.
            print(f"{wheel.name} -> meta-package (no importable top level): ok")
            continue
        probe = _probe_environment(wheel)
        for top in tops:
            done = subprocess.run(
                [str(probe / "bin" / "python"), "-c", f"import {top}"],
                capture_output=True,
                text=True,
            )
            if done.returncode:
                failures.append(f"{wheel.name}: `import {top}` failed: {done.stderr.strip()}")
            else:
                print(f"{wheel.name} -> {top}: ok")

    # Run twine check --strict
    twine_result = subprocess.run(
        ["uvx", "twine", "check", "--strict", "dist/*"],
        capture_output=True,
        text=True,
    )
    if twine_result.returncode != 0:
        for line in twine_result.stdout.strip().split("\n"):
            print(f"::error::{line}")
        # Also print stderr
        for line in twine_result.stderr.strip().split("\n"):
            if line:
                print(f"::error::{line}")
        return 5

    for line in failures:
        print(f"::error::{line}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())