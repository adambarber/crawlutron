"""The workspace binding must expose the member src roots, ahead of the venv's
editable installs, so an interpreter started in a workspace imports that tree."""

from __future__ import annotations

import pathlib

from scripts.bind_workspace_tree import PTH_NAME, bind, member_src_roots
from tests._walk_primitives import REPO_ROOT, package_roots


def test_the_binding_name_sorts_before_the_editable_installs() -> None:
    """Site appends pth paths in name order, so the workspace only wins if it sorts first."""
    assert PTH_NAME < "_editable_impl_crawlutron_core.pth"


def test_the_bound_roots_are_the_manifest_members_src_roots() -> None:
    """The derivation is the manifest's, not a literal list: a new member is bound too."""
    expected = tuple(sorted(REPO_ROOT / member / "src" for member in package_roots()))
    assert member_src_roots(REPO_ROOT) == expected


def test_bind_writes_one_absolute_root_per_line(tmp_path: pathlib.Path) -> None:
    written = bind(REPO_ROOT, tmp_path)
    assert written == tmp_path / PTH_NAME
    roots = written.read_text().splitlines()
    assert roots == [str(root) for root in member_src_roots(REPO_ROOT)]
    assert all(pathlib.Path(root).is_absolute() for root in roots)
