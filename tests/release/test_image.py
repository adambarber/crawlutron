"""Tests for scripts/release/image.py."""

from __future__ import annotations

from pathlib import Path

from tests.release._test_base import _make_script_runner

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "release" / "image.py"
_run_script = _make_script_runner("image.py")


class TestImage:
    """Tests for the image.py entrypoint."""

    def test_noop_when_native_version_empty(self) -> None:
        """Empty NATIVE_VERSION → no-op, exit 0."""
        result = _run_script({"NATIVE_VERSION": "", "GITHUB_REPOSITORY_OWNER": "test-owner"})
        assert result.returncode == 0
        assert "root not released; image entrypoint no-op" in result.stdout

    def test_missing_repository_owner_is_error(self) -> None:
        """Missing GITHUB_REPOSITORY_OWNER → exit 2."""
        result = _run_script({"NATIVE_VERSION": "0.5.2"})
        assert result.returncode == 2
        assert "GITHUB_REPOSITORY_OWNER environment variable is required" in result.stderr

    def test_missing_github_actor_token_is_error(self) -> None:
        """Missing GITHUB_ACTOR or GITHUB_TOKEN → exit 2."""
        result = _run_script(
            {
                "NATIVE_VERSION": "0.5.2",
                "GITHUB_REPOSITORY_OWNER": "test-owner",
            }
        )
        assert result.returncode == 2
        assert "GITHUB_ACTOR environment variable is required" in result.stderr

    def test_version_with_plus_sign_uses_dash_in_image_tag(self) -> None:
        """A local-version plus becomes a dash in the OCI tag."""
        from tests.release._script_loader import load_script

        image = load_script("image")
        assert image._image_tag("0.5.2+DEV", "owner") == ("ghcr.io/owner/crawlutron:0.5.2-dev")
