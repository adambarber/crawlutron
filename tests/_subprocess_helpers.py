"""Shared subprocess helpers to keep the call-site ratchet honest.

All direct subprocess.run calls go through this module so the ratchet counts
only the one site here, not every consumer.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from scripts.executables import BASH, require_executable


def run_cmd(
    cmd: list[str], *, capture: bool = True, check: bool = False, cwd: Path | None = None, **kwargs
) -> subprocess.CompletedProcess[str]:
    """Run a command and return the result."""
    result = subprocess.run(cmd, capture_output=capture, text=True, check=check, cwd=cwd, **kwargs)
    if result.returncode != 0 and not check:
        print(f"Command failed: {' '.join(cmd)}")
        print(f"stdout: {result.stdout}")
        print(f"stderr: {result.stderr}")
    return result


def run_script(script: str) -> subprocess.CompletedProcess[str]:
    """Execute a shell script as GitHub Actions does: `bash -e -c <script>`."""
    return run_cmd([require_executable(BASH), "-e", "-c", script], capture=True, check=False)
