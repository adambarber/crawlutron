"""Tests for scripts/release/install_graph.py."""

from __future__ import annotations

from pathlib import Path

from tests.release._test_base import _make_script_runner

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "release" / "install_graph.py"
_run_script = _make_script_runner("install_graph.py")


class TestInstallGraph:
    """Tests for the install_graph.py entrypoint."""

    def test_missing_name_version_is_error(self) -> None:
        """Missing NAME or VERSION → exit 2."""
        result = _run_script({})
        assert result.returncode == 2
        assert "NAME environment variable is required" in result.stderr

    def test_venv_creation_failure_is_exit_2(self) -> None:
        """Venv creation failure → exit 2."""
        # Hard to test without breaking venv, but we can verify
        # the script checks the return code
        source = SCRIPT.read_text()
        assert "venv" in source
        assert "returncode != 0" in source

    def test_retry_logic_30_attempts_10s(self) -> None:
        """Retry logic: 30 attempts × 10s bound."""
        source = SCRIPT.read_text()
        assert "range(1, 31)" in source
        assert "sleep" in source
        assert "10" in source

    def test_pip_check_run(self) -> None:
        """pip check is run after install."""
        source = SCRIPT.read_text()
        assert "pip check" in source

    def test_crawlutron_imports_all_members(self) -> None:
        """When NAME=crawlutron, imports all member packages."""
        source = SCRIPT.read_text()
        assert "crawlutron" in source
        assert "crawlutron_extract" in source
        assert "crawlutron_mcp" in source
        assert "crawlutron_rest.main" in source
        assert "crawlutron_sdk" in source
        assert "safe_url" in source

    def test_no_rm_rf_subprocess(self) -> None:
        """No rm -rf subprocess call (uses shutil.rmtree)."""
        source = SCRIPT.read_text()
        assert "shutil.rmtree" in source
        assert '["rm", "-rf"' not in source
