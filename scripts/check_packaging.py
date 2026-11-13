"""Prove every built wheel is installable and importable.

`twine check` passes on an unusable wheel: an undeclared dependency, a wrong
`packages` list. Installing it and importing what it ships is the real check.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys
import tempfile
import zipfile

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PROBE_ROOT = pathlib.Path(tempfile.gettempdir()) / "crawlutron-probes"
UV = shutil.which("uv") or "uv"


def _probe_environment(wheel: pathlib.Path) -> pathlib.Path:
    """A clean 3.14 environment holding exactly this wheel."""
    probe = PROBE_ROOT / wheel.stem
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
            # The app wheel depends on its members, which no index carries yet:
            # resolve siblings from the same dist/ while the rest comes from the index.
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
    """The import names this wheel ships, read from the wheel's own contents.

    Read from the archive, not installed metadata: the env also holds the wheel's
    dependencies, so crediting those to it would pass while its package is broken.
    """
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
    wheels = sorted((REPO_ROOT / "dist").glob("*.whl"))
    if not wheels:
        print("no wheels in dist/: run `uv build --all-packages --out-dir dist` first")
        return 1

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

    for line in failures:
        print(f"::error::{line}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
