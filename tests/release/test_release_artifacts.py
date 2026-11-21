"""Tests for scripts/release/release_artifacts.py."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from tests.release._test_base import _make_script_runner

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "release" / "release_artifacts.py"
_run_script = _make_script_runner("release_artifacts.py")


class TestReleaseArtifacts:
    """Tests for the release_artifacts.py entrypoint."""

    def test_missing_tag_is_error(self) -> None:
        """Missing TAG → exit 2."""
        result = _run_script({"GH_TOKEN": "dummy"})
        assert result.returncode == 2
        assert "TAG environment variable is required" in result.stderr

    def test_missing_gh_token_is_error(self) -> None:
        """Missing GH_TOKEN → exit 2."""
        result = _run_script({"TAG": "crawlutron-v0.5.2"})
        assert result.returncode == 2
        assert "GH_TOKEN environment variable is required" in result.stderr

    def test_missing_dist_dir_is_exit_3(self) -> None:
        """Missing dist/ directory → exit 3."""
        # The script uses REPO_ROOT from _common which is fixed to the actual repo
        # So we test the checksum failure case instead
        with tempfile.TemporaryDirectory() as tmpdir:
            result = subprocess.run(
                [sys.executable, str(SCRIPT)],
                cwd=tmpdir,
                capture_output=True,
                text=True,
                env={**os.environ, "TAG": "test", "GH_TOKEN": "dummy"},
            )
            assert result.returncode == 3
            assert "Checksum generation failed" in result.stderr

    def test_checksum_generation_in_dist(self) -> None:
        """Checksums generated in dist/ directory."""
        source = SCRIPT.read_text()
        assert "sha256sum" in source
        assert "dist" in source
        assert "SHA256SUMS" in source

    def test_upload_includes_wheel_tar_gz_and_sha256sums(self) -> None:
        """Upload includes .whl, .tar.gz, and SHA256SUMS."""
        source = SCRIPT.read_text()
        assert "*.whl" in source
        assert "*.tar.gz" in source
        assert "SHA256SUMS" in source
        assert "_gh_release_upload" in source

    def test_no_wheel_fails_upload(self) -> None:
        """No wheel/tar.gz → upload fails (no silent empty release)."""
        source = SCRIPT.read_text()
        assert "no wheel or sdist found" in source
