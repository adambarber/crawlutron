"""Resolve external tools to absolute paths, once, for the tests that spawn them.

`subprocess` resolves a bare name against `PATH` at exec time, so a test that
writes `["git", "rev-parse", ...]` binds its meaning to whatever the ambient
`PATH` happens to hold -- and when the tool is missing the failure surfaces as a
bare `FileNotFoundError` from inside `subprocess`, which reads like a bug in the
test rather than a missing dependency. Resolving up front names the dependency
and fails where it is declared.

Ruff's S607 (`start-process-with-partial-path`) is what this exists to satisfy,
and it is the one process rule in this suite that code can actually answer: a
command that cannot be written as a list of string literals is a different rule
(S603) with a different answer, and that one is argued in `pyproject.toml`.
"""

import shutil
from pathlib import Path

# Tools resolved more than once across the gate tests, so their absence is a
# setup problem worth naming once rather than per call site.
GIT = "git"
BASH = "bash"


def require_executable(name: str) -> str:
    """Absolute path to ``name``, or fail with the dependency named.

    Raises:
        RuntimeError: ``name`` is not on ``PATH``. A gate test that needs a tool
            this way should say so; failing with the name beats failing with a
            `FileNotFoundError` attributed to a subprocess call.
    """
    resolved = shutil.which(name)
    if resolved is None:
        raise RuntimeError(f"{name!r} is required by this test but was not found on PATH")
    return resolved


def require_repo_tool(service_root: Path, name: str) -> str:
    """Absolute path to a gate tool installed for this repository.

    The gate tools -- ``jscpd``, ``pre-commit`` -- arrive two different ways, and
    a hardcoded ``service_root/.venv/bin/<tool>`` is right in only one of them.
    ``CONTRIBUTING.md`` and the local workflow build a venv at the repository
    root; CI installs ``.[dev]`` into the runner's own Python, where the console
    script lands on ``PATH`` and no ``.venv`` exists at all. Both are correct
    installs of the same pinned version, so the tool is looked for in the venv
    first and on ``PATH`` second.

    Hardcoding the venv is not a style preference here. ``.venv`` is a symlink
    in a worktree and absent in CI, so the path was either dangling or missing,
    and the result was a ``FileNotFoundError`` naming a directory that was never
    where the tool was. A gate test that cannot find its dependency is not
    evidence about the gate, so this names the dependency instead.

    ``service_root`` is passed rather than derived from this module so a test
    that points it at a mirrored tree keeps pointing it at the machine's venv:
    the tool is a property of the machine, not of the tree being scanned.
    """
    venv_tool = service_root / ".venv" / "bin" / name
    if venv_tool.exists():
        return str(venv_tool)
    return require_executable(name)
