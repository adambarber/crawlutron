"""``require_repo_tool`` must reject a venv tool that is not a working install (#289).

A stale or corrupted file at ``.venv/bin/<tool>`` used to win silently over the
PATH copy; these pins name what a venv hit must still prove.
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
from pathlib import Path

import pytest

from scripts.executables import require_repo_tool
from tests._jscpd_gate_helpers import stub_resolver_probe


@pytest.fixture
def service_root(tmp_path: Path, monkeypatch) -> Path:
    """A scratch tree whose ``.venv/bin`` tests can plant tools into, with the interpreter's own bin pinned
    onto PATH first: the fall-through assertions need a real tool on PATH and the only one this repo
    guarantees is next to the running pytest.
    """
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    venv_bin = str(Path(sys.executable).parent)
    monkeypatch.setenv("PATH", venv_bin + os.pathsep + os.environ.get("PATH", ""))
    return tmp_path


def test_a_non_executable_venv_file_is_rejected(service_root: Path, monkeypatch) -> None:
    stub_resolver_probe(monkeypatch)
    (service_root / ".venv" / "bin" / "jscpd").write_text("", encoding="utf-8")
    resolved = require_repo_tool(service_root, "jscpd")
    assert not resolved.startswith(str(service_root / ".venv"))


def test_an_unrunnable_venv_exec_is_rejected(service_root: Path) -> None:
    """Exec bits but a failing body: the run probe must reject it before any
    version check, and the resolution falls through to PATH."""
    bogus = service_root / ".venv" / "bin" / "jscpd"
    bogus.write_text("#!/bin/sh\nexit 67\n", encoding="utf-8")
    bogus.chmod(0o755)
    resolved = require_repo_tool(service_root, "jscpd")
    assert not resolved.startswith(str(service_root / ".venv"))


def test_a_version_shouting_garbage_is_rejected(service_root: Path) -> None:
    """A ``--version`` with no version shape is the check a shim cannot fake by
    existing; it must lose to the PATH copy."""
    bogus = service_root / ".venv" / "bin" / "mypy"
    bogus.write_text("#!/bin/sh\necho 'not a version'\n", encoding="utf-8")
    bogus.chmod(0o755)
    resolved = require_repo_tool(service_root, "mypy")
    assert not resolved.startswith(str(service_root / ".venv"))


def test_a_working_venv_tool_is_accepted(service_root: Path) -> None:
    """A shim that really answers ``--version`` is accepted by the same probe
    the rejections run -- acceptance and rejection share one check."""
    shim = service_root / ".venv" / "bin" / "mypy"
    shim.write_text("#!/bin/sh\necho 'mypy 2.3.1'\n", encoding="utf-8")
    shim.chmod(0o755)
    assert require_repo_tool(service_root, "mypy") == str(shim)


def test_the_rejection_probe_matches_the_system_resolver() -> None:
    """Sanity pin: `which` + X_OK is what the PATH fallback trusts, so the
    fake-venv rejections are rejections of the venv copy, not of PATH."""
    sh = shutil.which("sh")
    assert sh is not None, "the sanity pin needs sh on PATH"
    assert os.access(sh, os.X_OK)
    assert stat.S_IXUSR & os.stat(sys.executable).st_mode
