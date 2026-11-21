"""Tests for scripts/release/build_assert.py."""

from __future__ import annotations

from tests.chokepoint_paths import REPO_ROOT
from tests.release._test_base import _make_script_runner

SCRIPT = REPO_ROOT / "scripts" / "release" / "build_assert.py"
_run_script = _make_script_runner("build_assert.py")


class TestBuildAssert:
    """Tests for the build_assert.py entrypoint."""

    def test_missing_selector_version_is_error(self) -> None:
        """Missing SELECTOR or EXPECTED_VERSION → exit 2."""
        result = _run_script({})
        assert result.returncode == 2
        assert "SELECTOR environment variable is required" in result.stderr

    def test_build_failure_is_exit_3(self) -> None:
        """Build failure → exit 3."""
        result = _run_script(
            {
                "SELECTOR": "nonexistent-package",
                "EXPECTED_VERSION": "0.5.0",
            }
        )
        assert result.returncode == 3
        assert "Build failed" in result.stderr

    def test_uv_build_called_with_package_and_out_dir(self) -> None:
        """uv build called with --package and --out-dir dist."""
        source = SCRIPT.read_text()
        assert "UV" in source
        assert '"build"' in source
        assert "--package" in source
        assert "--out-dir" in source
        assert "dist" in source

    def test_version_assert_reads_metadata(self) -> None:
        """Version assert reads Version from .dist-info/METADATA."""
        source = SCRIPT.read_text()
        assert ".dist-info/METADATA" in source
        assert "email.message_from_bytes" in source
        assert '["Version"]' in source

    def test_twine_check_strict(self) -> None:
        """twine check --strict is run."""
        source = SCRIPT.read_text()
        assert "twine check --strict" in source
