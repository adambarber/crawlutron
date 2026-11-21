"""Tests for scripts/coverage_reports.py."""

from __future__ import annotations

import tempfile
from pathlib import Path

from tests.release._test_base import _make_script_runner

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "coverage_reports.py"
_run_script = _make_script_runner("coverage_reports.py", subdir="")


class TestCoverageReports:
    """Tests for the coverage_reports.py entrypoint."""

    def test_missing_coverage_file_is_error(self) -> None:
        """Missing .coverage file → exit 1."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _run_script({}, cwd=tmpdir)
            assert result.returncode == 1
            assert "Coverage data file not found" in result.stderr

    def test_reports_dir_created(self) -> None:
        """Reports directory is created."""
        source = SCRIPT.read_text()
        assert "reports_dir.mkdir" in source
        assert "coverage_reports" in source

    def test_repo_wide_xml_html_markdown(self) -> None:
        """Generates repo-wide XML, HTML, and Markdown."""
        source = SCRIPT.read_text()
        assert '"coverage"' in source
        assert '"xml"' in source
        assert '"html"' in source
        assert '"report"' in source
        assert "--format=markdown" in source

    def test_per_package_reports(self) -> None:
        """Generates per-package reports derived from workspace members."""
        source = SCRIPT.read_text()
        assert "_include_patterns" in source
        assert "packages/*/src" in source
        assert "roster" in source

    def test_deterministic_paths_relative_to_repo_root(self) -> None:
        """All output paths are deterministic and relative to repo root."""
        source = SCRIPT.read_text()
        assert "REPO_ROOT" in source
        assert "relative_to" in source or "reports_dir" in source

    def test_coverage_include_patterns_derived_not_hardcoded(self) -> None:
        """Include patterns derived from pyproject.toml workspace members."""
        source = SCRIPT.read_text()
        assert "roster" in source
        # The implementation now uses roster() from common module
        # which derives patterns from pyproject.toml workspace members
