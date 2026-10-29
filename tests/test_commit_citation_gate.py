"""The commit-msg gate bites: citation AND Conventional Commit format laws (#561)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parents[1]

UNCITED = "Fix the transport bug\n"
CITED = "Fix the transport bug\n\nCloses #492\n"
CONVENTIONAL_CITED = "fix(transport): the bug\n\nCloses #492\n"
NOT_CONVENTIONAL = "update stuff\n\nCloses #492\n"
BAD_TYPE = "wibble(transport): the bug\n\nCloses #492\n"


def _run(message: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    msg_file = tmp_path / "COMMIT_EDITMSG"
    msg_file.write_text(message, encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-m", "scripts.commit_citation_gate", str(msg_file)],
        cwd=SERVICE_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def test_an_uncited_message_fails_naming_the_file(tmp_path):
    completed = _run(UNCITED, tmp_path)
    assert completed.returncode != 0
    assert "COMMIT_EDITMSG" in completed.stderr
    assert "Closes #<number>" in completed.stderr


def test_a_cited_conventional_message_passes(tmp_path):
    completed = _run(CONVENTIONAL_CITED, tmp_path)
    assert completed.returncode == 0, completed.stderr


def test_a_non_conventional_subject_fails_naming_the_format_law(tmp_path):
    completed = _run(NOT_CONVENTIONAL, tmp_path)
    assert completed.returncode != 0
    assert "commit-format law" in completed.stderr.lower()
    assert "citation law" not in completed.stderr.lower()


def test_an_unknown_commit_type_fails_naming_the_format_law(tmp_path):
    completed = _run(BAD_TYPE, tmp_path)
    assert completed.returncode != 0
    assert "commit-format law" in completed.stderr.lower()


def test_an_uncited_message_names_the_citation_law_first(tmp_path):
    completed = _run(UNCITED, tmp_path)
    assert completed.returncode != 0
    assert "citation law" in completed.stderr.lower()


def test_a_missing_message_file_fails_loudly(tmp_path):
    completed = subprocess.run(
        [sys.executable, "-m", "scripts.commit_citation_gate", str(tmp_path / "absent")],
        cwd=SERVICE_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 2
    assert "cannot read" in completed.stderr
