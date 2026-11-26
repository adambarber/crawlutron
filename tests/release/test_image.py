"""Tests for scripts/release/image.py."""

from __future__ import annotations

import ast

from tests.chokepoint_paths import REPO_ROOT
from tests.release._test_base import _make_script_runner

SCRIPT = REPO_ROOT / "scripts" / "release" / "image.py"
_run_script = _make_script_runner("image.py")


class TestImage:
    """Tests for the image.py entrypoint."""

    def test_noop_when_native_version_empty(self) -> None:
        """Empty NATIVE_VERSION -> no-op, exit 0."""
        result = _run_script({"NATIVE_VERSION": "", "GITHUB_REPOSITORY_OWNER": "test-owner"})
        assert result.returncode == 0
        assert "root not released; image entrypoint no-op" in result.stdout

    def test_missing_repository_owner_is_error(self) -> None:
        """Missing GITHUB_REPOSITORY_OWNER -> exit 2."""
        result = _run_script({"NATIVE_VERSION": "0.5.2"}, unset=("GITHUB_REPOSITORY_OWNER",))
        assert result.returncode == 2
        assert "GITHUB_REPOSITORY_OWNER environment variable is required" in result.stderr

    def test_missing_github_actor_token_is_error(self) -> None:
        """Missing GITHUB_ACTOR or GITHUB_TOKEN -> exit 2."""
        result = _run_script(
            {
                "NATIVE_VERSION": "0.5.2",
                "GITHUB_REPOSITORY_OWNER": "test-owner",
            },
            unset=("GITHUB_ACTOR", "GITHUB_TOKEN"),
        )
        assert result.returncode == 2
        assert "GITHUB_ACTOR environment variable is required" in result.stderr

    def test_version_with_plus_sign_uses_dash_in_image_tag(self) -> None:
        """A local-version plus becomes a dash in the OCI tag."""
        from tests.release._script_loader import load_script

        image = load_script("image")
        assert image._image_tag("0.5.2+DEV", "owner") == ("ghcr.io/owner/crawlutron:0.5.2-dev")

    def test_check_baked_proxy_probe_compiles_and_has_port_assignment(self) -> None:
        """
        The probe string passed to _exec_python_probe in _check_baked_proxy must be valid
        Python that assigns the port and connects to the listener.
        This catches the comment-swallows-next-statement bug.
        """
        from tests.release._script_loader import load_script

        image = load_script("image")

        # Extract the probe by importing the module and getting the function's source
        import inspect

        probe_source = inspect.getsource(image._check_baked_proxy)

        # The probe is a string assigned to a variable named 'probe'
        # Find the probe string in the source
        probe_tree = ast.parse(probe_source)
        probe_str = None
        for node in ast.walk(probe_tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "probe":
                        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                            probe_str = node.value.value
                            break

        assert probe_str is not None, "_check_baked_proxy must assign a string to 'probe'"

        # Verify the probe compiles
        compile(probe_str, "<probe>", "exec")

        # Verify the probe has the port assignment and socket connection
        # (not swallowed by a comment)
        assert "port = int(url.split(':')[-1])" in probe_str, "Probe must assign port from URL"
        assert "socket.create_connection" in probe_str, "Probe must connect to the listener"
        # No external example.com dependency
        forbidden = "example.com"
        assert forbidden not in probe_str, "Probe must not depend on external DNS"
