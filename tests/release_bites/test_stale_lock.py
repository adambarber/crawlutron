"""Bite: stale lock — the declared `lock` gate refuses a stale uv.lock."""

from __future__ import annotations

import shlex
import shutil
import subprocess
from pathlib import Path

import yaml

_CONFIG = Path(__file__).resolve().parents[2] / ".pre-commit-config.yaml"
_GATE = ["uv", "lock", "--check"]
_MANIFEST = (
    '[project]\nname = "stale-probe"\nversion = "0.1.0"\nrequires-python = "{python}"\n'
    'dependencies = ["requests>=2.0.0"]\n'
)
# uv 0.13.9 emits this lockfile-needs-update message on `--check`; not "unsatisfiable".
_STALE_REFUSAL = "The lockfile at `uv.lock` needs to be updated, but `--check` was provided."


def _declared_lock_gate() -> list[str]:
    """The argv every commit's `lock` hook runs, read from the live config."""
    document = yaml.safe_load(_CONFIG.read_text(encoding="utf-8"))
    for repository in document["repos"]:
        for hook in repository.get("hooks", ()):
            entry = str(hook["entry"])
            if hook["id"] == "lock" and "-m scripts.gate_fanout lock" in entry:
                return shlex.split(entry.split(" -- ", 1)[1])
    raise AssertionError(f"no `lock` gate declared in {_CONFIG.name}")


def test_the_declared_lock_gate_refuses_a_stale_project(tmp_path: Path):
    """The gate is wired to `uv lock --check`, and that command refuses a stale lock."""
    assert _declared_lock_gate() == _GATE
    uv = shutil.which("uv")
    assert uv is not None, "uv must be on PATH for the lock gate"
    project = tmp_path / "stale"
    project.mkdir()
    manifest = project / "pyproject.toml"
    manifest.write_text(_MANIFEST.format(python=">=3.14"), encoding="utf-8")
    subprocess.run([uv, "lock"], cwd=str(project), capture_output=True, text=True, check=True)
    manifest.write_text(_MANIFEST.format(python=">=3.13"), encoding="utf-8")

    result = subprocess.run([uv, "lock", "--check"], cwd=str(project), capture_output=True, text=True)

    assert result.returncode == 1, f"stale lock accepted: {result.stdout}{result.stderr}"
    assert _STALE_REFUSAL in result.stderr + result.stdout, (
        f"Expected the lockfile staleness refusal, got: {result.stdout}{result.stderr}"
    )
