"""Install docs must recommend the workspace-aware command, never the broken bare-pip one."""

from __future__ import annotations

import pathlib
import re

from tests.chokepoint_paths import REPO_ROOT
from tests.chokepoint_walks import discovered_files

_BROKEN_INSTALL = re.compile(r"pip\s+install\s+-e\s+['\"]?\.\[dev\]['\"]?")


def _tracked_docs(root: pathlib.Path = REPO_ROOT) -> tuple[str, ...]:
    """Every git-tracked markdown doc, derived so a new doc arrives covered."""
    return tuple(path.relative_to(root).as_posix() for path in discovered_files(root, "*.md"))


def _broken_install_offenders(
    root: pathlib.Path = REPO_ROOT, documents: tuple[str, ...] | None = None
) -> list[str]:
    """Every tracked doc line that presents the broken bare-pip root install as setup."""
    names = _tracked_docs(root) if documents is None else documents
    contents = discovered_files(root, "*.md", include_content=True)
    offenders: list[str] = []
    for name in names:
        document = root / name
        for number, line in enumerate(contents[document].splitlines(), start=1):
            if _BROKEN_INSTALL.search(line):
                offenders.append(f"{name}:{number} recommends the broken bare-pip root install")
    return offenders


def test_no_tracked_doc_names_the_broken_pip_install():
    assert not (failures := _broken_install_offenders()), "; ".join(failures)


def test_broken_install_detector_bites(tmp_path: pathlib.Path):
    """A restored broken line is named with its file and line number."""
    path = tmp_path / "install.md"
    path.write_text('setup\n\npip install -e ".[dev]"\n', encoding="utf-8")
    offenders = _broken_install_offenders(root=tmp_path, documents=("install.md",))
    assert len(offenders) == 1, offenders
    assert "install.md:3" in offenders[0], offenders


def test_broken_install_detector_spares_uv_commands(tmp_path: pathlib.Path):
    """uv-based commands and prose about pip are not the broken command."""
    path = tmp_path / "install.md"
    path.write_text(
        "uv sync --extra dev\nuv pip install -e packages/*\nplain pip ignores [tool.uv.sources]\n",
        encoding="utf-8",
    )
    assert _broken_install_offenders(root=tmp_path, documents=("install.md",)) == []
