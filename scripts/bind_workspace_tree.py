"""Bind a workspace's member src roots ahead of the shared venv's editable installs.

Site appends ``.pth`` paths in filename order: the ``!`` prefix sorts the
workspace first, without it the main checkout's packages win.
"""

from __future__ import annotations

import pathlib
import sysconfig
import tomllib

PTH_NAME = "!hardstand-workspace.pth"


def _members(root: pathlib.Path) -> tuple[str, ...]:
    """The uv workspace member patterns declared by the root manifest."""
    table = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    members = table["tool"]["uv"]["workspace"]["members"]
    return tuple(str(member) for member in members)


def _expanded(root: pathlib.Path, pattern: str) -> tuple[pathlib.Path, ...]:
    """One member pattern as the dirs it names; a glob expands to its matches."""
    if (root / pattern).is_dir():
        return (root / pattern,)
    return tuple(sorted(path for path in root.glob(pattern) if path.is_dir()))


def member_src_roots(root: pathlib.Path) -> tuple[pathlib.Path, ...]:
    """Every declared member's importable root, sorted."""
    src_roots = [member / "src" for pattern in _members(root) for member in _expanded(root, pattern)]
    return tuple(sorted(src for src in src_roots if src.is_dir()))


def prepend_member_roots(root: pathlib.Path, search_path: list[str]) -> None:
    """Put the invoked tree ahead of any stale shared-venv binding (#1230)."""
    roots = [str(src) for src in member_src_roots(root)]
    search_path[:] = roots + [entry for entry in search_path if entry not in roots]


def bind(root: pathlib.Path, site_packages: pathlib.Path) -> pathlib.Path:
    """Write the workspace's roots into the pth the interpreter loads first."""
    target = site_packages / PTH_NAME
    target.write_text("".join(f"{src}\n" for src in member_src_roots(root)), encoding="utf-8")
    return target


def main() -> int:
    """Bind the current directory's workspace into this interpreter's site dir."""
    root = pathlib.Path.cwd()
    target = bind(root, pathlib.Path(sysconfig.get_paths()["purelib"]))
    print(f"bound {root} -> {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
