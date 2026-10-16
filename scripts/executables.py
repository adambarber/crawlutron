"""Resolve external tools to absolute paths, once, for the tests that spawn them.

``subprocess`` resolves names at exec time (missing tools error inside it); resolving up front
names the dependency -- ``require_executable`` fails naming it, ``require_repo_tool`` venv-first.
"""

import shutil
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

    Gate tools (``jscpd``, ``pre-commit``) arrive via a root venv or CI's ``PATH``; ``service_root``
    is passed so a mirrored tree still points at the machine's venv.
    """
    venv_tool = service_root / ".venv" / "bin" / name
    if venv_tool.exists():
        return str(venv_tool)
    return require_executable(name)
