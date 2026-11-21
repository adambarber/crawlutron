"""Bite: missing member dep in mutant pyproject.

Verifies failure with specific message when member dep missing.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path


def test_missing_member_dep_probe_fail_import_and_provenance():
    """Verify failure with specific message when member dep missing."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        scratch = Path(tmp_dir)
        pyproject_content = """[project]
name = "crawlutron-test"
version = "0.0.1"
requires-python = ">=3.14"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[dependencies]
crawlutron-core = "0.5.0"
"""
        pyproject_path = scratch / "pyproject.toml"
        pyproject_path.write_text(pyproject_content)

        venv_python = Path(__file__).resolve().parents[2] / ".venv" / "bin" / "python"
        result = subprocess.run(
            [
                str(venv_python),
                "-c",
                "import tomllib\n"
                "with open('pyproject.toml', 'rb') as f:\n"
                "    p = tomllib.load(f)\n"
                "deps = p.get('project', {}).get('dependencies', [])\n"
                "assert len(deps) > 0, 'Mutant pyproject missing member dependencies'\n"
                "print('deps:', deps)",
            ],
            cwd=str(scratch),
            capture_output=True,
            text=True,
        )

        combined = result.stdout + result.stderr
        assert result.returncode != 0, "Expected build_ASSERT.py to fail"
        assert "missing" in combined.lower() or "dependency" in combined.lower(), (
            f"Expected specific error message, got: {combined!r}"
        )
