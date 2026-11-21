"""Bite: image version 0.0.0.

Verifies version 0.0.0 detection fails with specific message.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path


def test_image_version_0_0_0_specific_assert():
    """Verify version 0.0.0 detection fails with specific message."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        scratch = Path(tmp_dir)
        pyproject_content = """[project]
name = "crawlutron-test"
version = "0.0.0"
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
                "v = p['project']['version']\n"
                "print('version=' + v)\n"
                "import sys\n"
                "sys.exit(1) if v == '0.0.0' else sys.exit(0)",
            ],
            cwd=str(scratch),
            capture_output=True,
            text=True,
        )

        combined = result.stdout + result.stderr
        assert "version=0.0.0" in combined, f"Expected version 0.0.0 in output, {combined!r}"
        assert result.returncode != 0, f"Expected non-zero exit for version 0.0.0, {combined!r}"
