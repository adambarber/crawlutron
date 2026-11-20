"""Every package's version is a declared number, and the runtime reads that one."""

from __future__ import annotations

import configparser
import tomllib
from pathlib import Path

import crawlutron
from tests._ci_contract import _jobs, job_steps
from tests._walk_primitives import workspace_package_dirs, workspace_project
from tests.invariants.test_mypy_scope import _jsonc_load_source

SERVICE_ROOT = Path(__file__).resolve().parents[1]


def test_every_package_declares_a_static_version() -> None:
    """release-please can only bump a number that exists in the manifest."""
    for path, directory in workspace_package_dirs(SERVICE_ROOT).items():
        project = workspace_project(directory)
        dynamic = project.get("dynamic", [])
        assert isinstance(dynamic, list)
        assert "version" not in dynamic, f"{path} still derives its version at build time"
        assert isinstance(project.get("version"), str), f"{path} declares no version"


def test_the_running_version_is_the_core_declaration() -> None:
    """One home for the number: the pyproject release-please rewrites."""
    assert crawlutron.__version__ == workspace_project(SERVICE_ROOT / "packages/core")["version"]


def test_every_python_version_pin_is_314() -> None:
    """One interpreter family: every checker, workflow and floor agree."""
    assert workspace_project(SERVICE_ROOT)["requires-python"] == ">=3.14"
    table = tomllib.loads((SERVICE_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    ruff = table["tool"]["ruff"]
    assert isinstance(ruff, dict) and ruff.get("target-version") == "py314", ruff
    for path, directory in workspace_package_dirs(SERVICE_ROOT).items():
        assert workspace_project(directory)["requires-python"] == ">=3.14", path

    mypy = configparser.ConfigParser()
    mypy.read(SERVICE_ROOT / "mypy.ini", encoding="utf-8")
    assert mypy["mypy"]["python_version"] == "3.14"
    pyright = _jsonc_load_source((SERVICE_ROOT / "pyrightconfig.json").read_text(encoding="utf-8"))
    assert pyright["pythonVersion"] == "3.14"
    assert (SERVICE_ROOT / ".python-version").read_text(encoding="utf-8").strip().startswith("3.14")
    lock = tomllib.loads((SERVICE_ROOT / "uv.lock").read_text(encoding="utf-8"))
    assert lock["requires-python"] == ">=3.14"

    for name in ("ci.yml", "release.yml", "packaging.yml"):
        text = (SERVICE_ROOT / ".github/workflows" / name).read_text(encoding="utf-8")
        jobs = _jobs(text)
        versions: set[str] = set()
        for job_name, job in jobs.items():
            if "steps" not in job:
                continue  # a reusable job declares `uses`, not steps
            for step in job_steps(text, job_name):
                with_block = step.get("with")
                if isinstance(with_block, dict) and "python-version" in with_block:
                    versions.add(str(with_block["python-version"]))
        assert versions and versions == {"3.14"}, f"{name}: {versions}"
