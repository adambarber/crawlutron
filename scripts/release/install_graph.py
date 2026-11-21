"""Install-graph probe + retry for released packages.

This replaces the `install-graph` job from release.yml.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

from _common import _require_env, _run


def _validate_env() -> tuple[str, str]:
    name = _require_env("NAME")
    version = _require_env("VERSION")
    return name, version


def _create_venv(probe_dir: Path) -> tuple[str, str]:
    if probe_dir.exists():
        shutil.rmtree(probe_dir, ignore_errors=True)

    venv_result = _run(
        [sys.executable, "-m", "venv", str(probe_dir)],
        capture=True,
    )
    if venv_result.returncode != 0:
        print(f"Venv creation failed: {venv_result.stderr}", file=sys.stderr)
        sys.exit(2)

    pip = str(probe_dir / "bin" / "pip")
    python = str(probe_dir / "bin" / "python")
    return pip, python


def _install_with_retry(pip: str, name: str, version: str) -> None:
    installed = False
    for attempt in range(1, 31):
        result = _run(
            [pip, "install", "--quiet", "--no-cache-dir", f"{name}=={version}"],
            capture=True,
        )
        if result.returncode == 0:
            installed = True
            break
        if attempt == 30:
            print(f"::error::{name}=={version} never resolved from the published index", file=sys.stderr)
            sys.exit(3)
        _run(["sleep", "10"], capture=True)

    if not installed:
        sys.exit(3)


def _pip_check(pip: str) -> None:
    pip_check = _run([pip, "check"], capture=True)
    if pip_check.returncode != 0:
        print(f"pip check failed: {pip_check.stderr}", file=sys.stderr)
        sys.exit(4)


def _import_members(python: str) -> None:
    member_packages = [
        "crawlutron",
        "crawlutron_extract",
        "crawlutron_mcp",
        "crawlutron_rest.main",
        "crawlutron_sdk",
        "safe_url",
    ]
    for pkg in member_packages:
        import_result = _run(
            [python, "-c", f"import {pkg}"],
            capture=True,
        )
        if import_result.returncode != 0:
            print(f"Import failed for {pkg}: {import_result.stderr}", file=sys.stderr)
            sys.exit(4)


def main() -> int:
    name, version = _validate_env()
    probe_dir = Path(tempfile.mkdtemp(prefix="crawlutron-probe-"))

    pip, python = _create_venv(probe_dir)
    _install_with_retry(pip, name, version)
    _pip_check(pip)

    if name == "crawlutron":
        _import_members(python)

    print(f"Package {name}=={version} installed and verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
