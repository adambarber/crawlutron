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
