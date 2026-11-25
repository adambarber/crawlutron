"""Prove every built wheel is installable and importable.

`twine check` passes on an unusable wheel: an undeclared dependency, a wrong
`packages` list. Installing it and importing what it ships is the real check.
"""

from __future__ import annotations

import configparser
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import zipfile

from utils.paths import REPO_ROOT

PROBE_ROOT = pathlib.Path(tempfile.gettempdir()) / "crawlutron-probes"
UV = shutil.which("uv") or "uv"


def _probe_environment(wheel: pathlib.Path) -> pathlib.Path:
    """A clean 3.12 environment holding exactly this wheel."""
    probe = PROBE_ROOT / wheel.stem
    if not (probe / "bin" / "python").exists():
        subprocess.run(
            [UV, "venv", "--python", "3.12", str(probe)],
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


def _declared_scripts(wheel: pathlib.Path) -> list[tuple[str, str]]:
    """Console scripts declared by the wheel, as (command, module:attr) pairs."""
    with zipfile.ZipFile(wheel) as archive:
        entries = [name for name in archive.namelist() if name.endswith(".dist-info/entry_points.txt")]
        if not entries:
            return []
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(archive.read(entries[0]).decode("utf-8"))
    if not parser.has_section("console_scripts"):
        return []
    return [(command, target) for command, target in parser.items("console_scripts")]


def _probe_import(
    probe: pathlib.Path, code: str, *, cwd: pathlib.Path | None = None, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run one import probe in the wheel's clean environment."""
    return subprocess.run(
        [str(probe / "bin" / "python"), "-c", code],
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        capture_output=True,
        text=True,
    )


def _script_import_error(probe: pathlib.Path, command: str, target: str) -> str | None:
    """None when the target imports from the wheel env, else a named gate failure."""
    module, separator, attribute = target.partition(":")
    if not separator or not module or not attribute:
        return f"pyproject.toml [project.scripts] {command}: malformed target {target!r}"
    statements = ["import importlib", f"obj = importlib.import_module({module!r})"]
    statements.extend(f"obj = getattr(obj, {part!r})" for part in attribute.split("."))
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    done = _probe_import(probe, "; ".join(statements), cwd=probe, env=env)
    if done.returncode:
        return (
            f"pyproject.toml [project.scripts] {command} = {target}: "
            f"not importable from the wheel: {done.stderr.strip()}"
        )
    return None


def main() -> int:
    """Every wheel in dist/ installs into its own environment and imports."""
    wheels = sorted((REPO_ROOT / "dist").glob("*.whl"))
    if not wheels:
        print("no wheels in dist/: run `uv build --all-packages --out-dir dist` first")
        return 1

    failures: list[str] = []
    for wheel in wheels:
        tops = _own_top_levels(wheel)
        scripts = _declared_scripts(wheel)
        if not tops and not scripts:
            # No code and no entry points: the app wheel pulls its members in,
            # with nothing of its own to import or launch.
            print(f"{wheel.name} -> meta-package (no importable top level): ok")
            continue
        probe = _probe_environment(wheel)
        for top in tops:
            done = _probe_import(probe, f"import {top}")
            if done.returncode:
                failures.append(f"{wheel.name}: `import {top}` failed: {done.stderr.strip()}")
            else:
                print(f"{wheel.name} -> {top}: ok")
        for command, target in scripts:
            error = _script_import_error(probe, command, target)
            if error:
                failures.append(f"{wheel.name}: {error}")
            else:
                print(f"{wheel.name} -> script {command}: ok")

    for line in failures:
        print(f"::error::{line}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
