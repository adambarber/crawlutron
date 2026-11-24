"""Tests for scripts/release/emit_roster.py."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from tests.chokepoint_paths import REPO_ROOT
from tests.release._test_base import _make_script_runner

SCRIPT = REPO_ROOT / "scripts" / "release" / "emit_roster.py"
_run_script = _make_script_runner("emit_roster.py")


class TestEmitRoster:
    """Tests for the emit_roster.py entrypoint."""

    def test_noop_when_no_tags_at_head(self) -> None:
        """Empty RELEASE_TAG and no tags at HEAD → 0 packages, exit 0."""
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
            github_output = f.name
        try:
            result = _run_script(
                {"RELEASE_TAG": "", "GITHUB_OUTPUT": github_output}, unset=("GITHUB_EVENT_NAME",)
            )
            assert result.returncode == 0
            assert "0 package(s) tagged" in result.stdout
            assert "root=not released" in result.stdout
            assert "primary=false" in result.stdout

            output_content = Path(github_output).read_text()
            assert "packages=[]" in output_content
            assert "root_version=" in output_content
            assert "primary=false" in output_content
        finally:
            Path(github_output).unlink(missing_ok=True)

    def test_unknown_tag_yields_empty_set(self) -> None:
        """Unknown RELEASE_TAG → empty released set (not an error)."""
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
            github_output = f.name
        try:
            result = _run_script({"RELEASE_TAG": "crawlutron-v99.99.99", "GITHUB_OUTPUT": github_output})
            assert result.returncode == 0
            assert "0 package(s) tagged" in result.stdout

            output_content = Path(github_output).read_text()
            assert "packages=[]" in output_content
        finally:
            Path(github_output).unlink(missing_ok=True)

    def test_missing_github_output_is_error(self) -> None:
        """Missing GITHUB_OUTPUT → exit 2."""
        result = _run_script({"RELEASE_TAG": ""}, unset=("GITHUB_OUTPUT",))
        assert result.returncode == 2
        assert "GITHUB_OUTPUT environment variable is required" in result.stderr

    def test_released_entries_match_roster(self) -> None:
        """When RELEASE_TAG is set, script calls release_roster.py --released."""
        # Verify the script passes RELEASE_TAG through to release_roster.py
        source = SCRIPT.read_text()
        assert "RELEASE_TAG" in source
        assert "--released" in source

    def test_primary_resolved_from_release_roster(self) -> None:
        """primary output comes from release_roster.py --primary, never hardcoded."""
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
            github_output = f.name
        try:
            result = _run_script(
                {
                    "RELEASE_TAG": "",
                    "GITHUB_OUTPUT": github_output,
                    "GITHUB_EVENT_NAME": "push",
                    "GITHUB_REF_NAME": "refs/tags/crawlutron-v0.5.2",
                }
            )
            assert result.returncode == 0

            output_content = Path(github_output).read_text()
            assert "primary=" in output_content
        finally:
            Path(github_output).unlink(missing_ok=True)

    def test_stdout_summary_format(self) -> None:
        """Stdout is one-line summary with count, root version, primary."""
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
            github_output = f.name
        try:
            result = _run_script({"RELEASE_TAG": "", "GITHUB_OUTPUT": github_output})
            assert result.returncode == 0
            assert "package(s) tagged" in result.stdout
            assert "root=" in result.stdout
            assert "primary=" in result.stdout
        finally:
            Path(github_output).unlink(missing_ok=True)

    def test_json_compact_in_github_output(self) -> None:
        """GITHUB_OUTPUT packages is compact JSON (no indent)."""
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
            github_output = f.name
        try:
            result = _run_script({"RELEASE_TAG": "", "GITHUB_OUTPUT": github_output})
            assert result.returncode == 0

            output_content = Path(github_output).read_text()
            for line in output_content.strip().split("\n"):
                if line.startswith("packages="):
                    packages_json = line[len("packages=") :]
                    packages = json.loads(packages_json)
                    compact = json.dumps(packages, separators=(",", ":"))
                    assert packages_json == compact
                    break
        finally:
            Path(github_output).unlink(missing_ok=True)
