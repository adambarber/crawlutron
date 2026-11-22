"""Tests for scripts/release/sbom.py."""

from __future__ import annotations

from tests.chokepoint_paths import REPO_ROOT
from tests.release._test_base import _make_script_runner

SCRIPT = REPO_ROOT / "scripts" / "release" / "sbom.py"
_run_script = _make_script_runner("sbom.py")


class TestSbom:
    """Tests for the sbom.py entrypoint."""

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

    def test_lock_export_uses_frozen(self) -> None:
        """uv export uses --frozen flag (no lock regeneration)."""
        # Can't test full flow without gh CLI; verify script has --frozen
        source = SCRIPT.read_text()
        assert "--frozen" in source
        assert '"export"' in source
        assert "UV" in source
        assert "uv lock" not in source

    def test_cyclonedx_bom_version_pinned(self) -> None:
        """cyclonedx-bom version is pinned to 8.0.1."""
        source = SCRIPT.read_text()
        assert "cyclonedx-bom==8.0.1" in source
