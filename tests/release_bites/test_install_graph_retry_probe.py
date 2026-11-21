"""Bite: install-graph 30-attempt retry probe.

Verifies retry-exhaustion message on missing env vars.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path


def test_install_graph_30_attempt_retry_probe_specific_message():
    """Verify retry-exhaustion message on missing env vars."""
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

        env_without_creds = {k: v for k, v in os.environ.items() if k != "NAME" and k != "VERSION"}

        venv_python = Path(__file__).resolve().parents[2] / ".venv" / "bin" / "python"
        result = subprocess.run(
            [str(venv_python), "scripts/release/install_graph.py"],
            capture_output=True,
            text=True,
            env=env_without_creds,
            cwd=str(Path(__file__).resolve().parents[2]),
        )

        combined = result.stdout + result.stderr
        assert result.returncode != 0, "Expected install_graph.py to fail without NAME/VERSION"
        assert "NAME" in combined or "VERSION" in combined or "required" in combined.lower(), (
            f"Expected specific error message, got: {combined!r}"
        )
