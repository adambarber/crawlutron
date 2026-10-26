"""Resolve external tools to absolute paths, once, for the tests that spawn them.

``subprocess`` resolves names at exec time (missing tools error inside it); resolving up front
names the dependency -- ``require_executable`` fails naming it, ``require_repo_tool`` venv-first.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

# Tools resolved more than once across the gate tests, so their absence is a
# setup problem worth naming once rather than per call site.
GIT = "git"
BASH = "bash"


def require_executable(name: str) -> str:
    """Absolute path to ``name``, or fail naming the dependency.

    A gate test that needs a tool this way should say so -- failing with the name
    beats a ``FileNotFoundError`` attributed to a subprocess call.
    """
    resolved = shutil.which(name)
    if resolved is None:
        raise RuntimeError(f"{name!r} is required by this test but was not found on PATH")
    return resolved


def require_repo_tool(service_root: Path, name: str) -> str:
    """Absolute path to a gate tool installed for this repository, venv-first then ``PATH``.

    A venv hit must prove it runs and answers ``--version`` (#289): a stale or corrupted
    file falls through to PATH instead of winning silently.
    """
    venv_tool = service_root / ".venv" / "bin" / name
    if _is_working_tool(venv_tool):
        return str(venv_tool)
    return require_executable(name)


# Tools whose --version output carries the product name; probed once per
# resolution, so a broken venv install fails naming the tool, not inside a gate.
_VERSIONED_TOOLS = frozenset({"jscpd", "pre-commit", "mypy", "ruff", "pyright", "vulture", "pytest"})


def _is_working_tool(candidate: Path) -> bool:
    """True when the candidate is an executable file that runs and answers ``--version``."""
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        return False
    if not _probe(candidate):
        return False
    return True


def _probe(candidate: Path) -> bool:
    """Run ``--version`` once; a tool that cannot answer is not the tool."""
    try:
        probe = subprocess.run(
            [str(candidate), "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if probe.returncode != 0:
        return False
    if candidate.name in _VERSIONED_TOOLS:
        return bool(re.search(r"\d+\.\d+", probe.stdout + probe.stderr))
    return True
