"""Bite: a wheel's declared console scripts must import from what that wheel ships."""

from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from tests.chokepoint_paths import REPO_ROOT

METADATA = "Metadata-Version: 2.1\nName: {name}\nVersion: 0.1\n"
WHEEL = "Wheel-Version: 1.0\nGenerator: bite\nRoot-Is-Purelib: true\nTag: py3-none-any\n"


def _write_wheel(dist: Path, name: str, entry_points: str, modules: dict[str, str]) -> None:
    """Build a minimal wheel holding optional modules and console-script declarations."""
    dist.mkdir(parents=True, exist_ok=True)
    wheel = dist / f"{name}-0.1-py3-none-any.whl"
    info = f"{name}-0.1.dist-info"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(f"{info}/METADATA", METADATA.format(name=name))
        archive.writestr(f"{info}/WHEEL", WHEEL)
        archive.writestr(f"{info}/entry_points.txt", entry_points)
        archive.writestr(f"{info}/RECORD", "")
        for path, source in modules.items():
            archive.writestr(path, source)


@pytest.fixture
def gate_tree(tmp_path: Path) -> Path:
    """A disposable repo shape so the gate installs wheels without touching the tree."""
    root = tmp_path / "gate_tree"
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "scripts" / "check_packaging.py", root / "scripts" / "check_packaging.py")
    for name in ("__init__.py",):
        shutil.copy2(REPO_ROOT / "scripts" / name, root / "scripts" / name)
    shutil.copytree(REPO_ROOT / "utils", root / "utils", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(REPO_ROOT / "pyproject.toml", root / "pyproject.toml")
    return root


def _run_gate(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "scripts.check_packaging"],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("name", "target", "modules"),
    [
        ("missing_module_probe", "scripts.pin_verify:main", {}),
        ("missing_attr_probe", "probe_pkg:missing", {"probe_pkg/__init__.py": "def main(): pass\n"}),
    ],
)
def test_a_declared_script_that_cannot_import_is_refused_by_name(
    gate_tree: Path, name: str, target: str, modules: dict[str, str]
) -> None:
    """A console script whose module or attribute is absent is refused, naming the declaration."""
    _write_wheel(gate_tree / "dist", name, f"[console_scripts]\nbroken-script = {target}\n", modules)
    result = _run_gate(gate_tree)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "pyproject.toml [project.scripts] broken-script" in result.stdout
    assert target in result.stdout


def test_a_declared_script_that_imports_is_accepted(gate_tree: Path) -> None:
    """A console script whose module and attribute ship in the wheel passes the gate."""
    _write_wheel(
        gate_tree / "dist",
        "good_script_probe",
        "[console_scripts]\nprobe-script = probe_pkg:main\n",
        {"probe_pkg/__init__.py": "def main(): pass\n"},
    )
    result = _run_gate(gate_tree)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "probe-script: ok" in result.stdout


def test_a_wheel_without_code_or_scripts_stays_a_meta_package(gate_tree: Path) -> None:
    """A wheel declaring neither modules nor scripts remains the honest meta-package case."""
    _write_wheel(gate_tree / "dist", "empty_probe", "", {})
    result = _run_gate(gate_tree)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "meta-package" in result.stdout
