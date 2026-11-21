"""Bite: stale lock.

Invokes real production config resolution (uv lock --check) against a
deliberately stale lock and asserts the specific refusal message.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

_UV = shutil.which("uv")


def test_stale_lock_refusal_message(tmp_path):
    """uv lock --check on a stale lock refuses with an unsatisfiable diagnostic."""
    assert _UV is not None, "uv must be on PATH to check a stale lock"
    scratch = Path(tmp_path)
    (scratch / "pyproject.toml").write_text(
        '[project]\nname = "stale-probe"\nversion = "0.1.0"\n'
        'requires-python = ">=3.14"\ndependencies = ["requests>=2.0.0"]\n'
    )
    # Generate a real lock, then stale the project constraint.
    subprocess.run([_UV, "lock"], capture_output=True, text=True, cwd=str(scratch), check=True)
    (scratch / "pyproject.toml").write_text(
        '[project]\nname = "stale-probe"\nversion = "0.1.0"\n'
        'requires-python = ">=3.14"\ndependencies = ["requests>=99.0.0"]\n'
    )

    result = subprocess.run(
        [_UV, "lock", "--check"],
        capture_output=True,
        text=True,
        cwd=str(scratch),
    )

    combined = result.stdout + result.stderr
    assert result.returncode != 0, f"Expected refusal, got: {combined!r}"
    assert "unsatisfiable" in combined.lower(), f"Expected 'unsatisfiable' refusal, got: {combined!r}"
