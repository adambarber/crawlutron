"""Shared test utilities for release scripts."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _run_script(
    script_path: Path,
    env: dict[str, str],
    cwd: Path | None = None,
    unset: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    """Run a release script with an explicit environment."""
    repo_root = script_path.resolve().parents[2]
    full_env = {key: value for key, value in os.environ.items() if key not in unset}
    full_env.update(env)
    return subprocess.run(
        [sys.executable, str(script_path)],
        cwd=str(cwd or repo_root),
        capture_output=True,
        text=True,
        env=full_env,
    )


def _make_script_runner(script_name: str, subdir: str = "release"):
    """Create a _run_script function for a specific release script."""
    script_path = Path(__file__).resolve().parents[2] / "scripts" / subdir / script_name

    def _runner(
        env: dict[str, str],
        cwd: Path | None = None,
        unset: tuple[str, ...] = (),
    ) -> subprocess.CompletedProcess[str]:
        return _run_script(script_path, env, cwd=cwd, unset=unset)

    return _runner
