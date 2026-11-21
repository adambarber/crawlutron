"""Bite: partial Dockerfile COPY. Verifies Docker build fails with specific error."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest


def test_partial_dockerfile_copy_structural_pattern():
    """Verify Docker build fails with specific error for missing COPY source."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        scratch = Path(tmp_dir)
        dockerfile_content = "FROM python:3.14-slim\n\nCOPY missing_source_file.txt /app/\n"
        dockerfile_content += "RUN pip install -r requirements.txt\n"
        dockerfile_path = scratch / "Dockerfile"
        dockerfile_path.write_text(dockerfile_content)

        dockerfile_text = dockerfile_path.read_text(encoding="utf-8")
        assert "COPY" in dockerfile_text, "Dockerfile should contain a COPY instruction"

        copy_lines = [line for line in dockerfile_text.splitlines() if line.strip().startswith("COPY")]
        assert len(copy_lines) > 0, "No COPY instruction found in Dockerfile"

        copy_line = copy_lines[0]
        assert "COPY" in copy_line, f"COPY line should contain COPY keyword: {copy_line!r}"

        parts = copy_line.split()
        assert len(parts) >= 3, f"COPY should have source and destination: {copy_line!r}"
        copy_source = parts[1]
        assert copy_source, f"COPY source should be a valid path: {copy_line!r}"

        docker_path = shutil.which("docker")
        if docker_path:
            result = subprocess.run(
                [docker_path, "build", "-f", str(dockerfile_path), "test-image"],
                capture_output=True,
                text=True,
                cwd=str(scratch),
            )

            combined = result.stdout + result.stderr
            if result.returncode != 0:
                assert (
                    "no such file" in combined.lower()
                    or "missing" in combined.lower()
                    or "context" in combined.lower()
                ), f"Expected specific error about missing source, got: {combined!r}"
        else:
            pytest.skip("docker not available")
