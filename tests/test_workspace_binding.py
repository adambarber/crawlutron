"""The invoked tree wins: member roots are derived, prepended, and guarded."""

from __future__ import annotations

import pathlib
import sys
import tomllib

from tests._walk_primitives import (
    REPO_ROOT,
    member_import_names,
    member_src_roots,
    prepend_member_roots,
)


def test_member_roots_are_manifest_derived() -> None:
    expected = tuple(sorted(REPO_ROOT / member / "src" for member in _package_roots()))
    assert member_src_roots(REPO_ROOT) == expected


def test_member_import_names_cover_every_wheel_package() -> None:
    names = member_import_names(REPO_ROOT)
    assert "crawlutron" in names
    assert "crawlutron_sdk" in names
    assert "crawlutron_core" not in names


def test_invoked_tree_is_prepended_ahead_of_a_stale_entry(tmp_path: pathlib.Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[tool.uv.workspace]\nmembers = ["packages/*"]\n')
    own = tmp_path / "packages" / "core" / "src"
    own.mkdir(parents=True)
    foreign = str(tmp_path / "foreign")
    search_path = [foreign]
    prepend_member_roots(tmp_path, search_path)
    assert search_path[0] == str(own)
    assert search_path[1:] == [foreign]


def test_the_session_guard_reports_a_foreign_module_origin() -> None:
    from tests import conftest

    impostor = pathlib.Path("/foreign/crawlutron/__init__.py")
    module = type(sys)("crawlutron")
    module.__file__ = str(impostor)
    original = sys.modules.get("crawlutron")
    sys.modules["crawlutron"] = module
    try:
        offenders = conftest._foreign_module_origins(REPO_ROOT)
        assert offenders == [("crawlutron", impostor)]
    finally:
        if original is None:
            del sys.modules["crawlutron"]
        else:
            sys.modules["crawlutron"] = original


def _hardstand_config() -> dict[str, object]:
    with (REPO_ROOT / "hardstand.toml").open("rb") as handle:
        return tomllib.load(handle)


def test_setup_creates_a_workspace_local_venv() -> None:
    setup = _hardstand_config()["setup"]
    assert isinstance(setup, list)
    names = {step["name"] for step in setup}
    assert "sync-venv" in names
    assert "link-venv" not in names
    assert "bind-workspace-tree" not in names
    sync = next(step for step in setup if step["name"] == "sync-venv")
    assert "rm .venv" in sync["run"]
    assert "uv sync --frozen --extra dev" in sync["run"]


def test_shell_action_sees_the_workspace_pythonpath() -> None:
    actions = _hardstand_config()["actions"]
    assert isinstance(actions, list)
    shell = next(action for action in actions if action["name"] == "shell")
    assert {"PATH", "PYTHONPATH"} <= set(shell["env"])


def _package_roots() -> tuple[str, ...]:
    from tests._walk_primitives import package_roots

    return package_roots()
