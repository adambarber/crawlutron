"""Regression tests for the duplication gate's fail-closed wrapper."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import _jscpd_gate as gate
import pytest


def _args(profile: str = "app", threshold: float = 0) -> argparse.Namespace:
    return argparse.Namespace(
        profile=profile,
        min_lines=5,
        min_tokens=50,
        threshold=threshold,
        fail_on_empty=True,
    )


def _write_expectations(path: Path, pairs: list[dict]) -> None:
    path.write_text(json.dumps({"version": 1, "pairs": pairs}), encoding="utf-8")


def _stub_report(monkeypatch, duplicate: dict, percentage: float = 0) -> None:
    report = {
        "statistics": {
            "total": {"sources": 50, "percentage": percentage, "duplicatedLines": 5}
        },
        "duplicates": [duplicate],
    }

    def run(command, **_kwargs):
        output = Path(command[command.index("--output") + 1])
        output.mkdir(exist_ok=True)
        (output / "jscpd-report.json").write_text(json.dumps(report), encoding="utf-8")
        return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(gate.subprocess, "run", run)


def _duplicate() -> dict:
    return {
        "firstFile": {"name": "new.py", "start": 10, "end": 20},
        "secondFile": {"name": "other.py", "start": 30, "end": 40},
        "fragment": "duplicate",
    }


def _valid_pair() -> dict:
    return {
        "id": "new-clone",
        "profiles": ["app"],
        "first": {"path": "app/new.py", "start": 10, "end": 20},
        "second": {"path": "app/other.py", "start": 30, "end": 40},
        "reason": "intentional",
        "guard": "tests/test_safety_invariants.py::test_crawl_error_members_derive_from_classification",
        "fragment_sha256": hashlib.sha256(b"duplicate").hexdigest(),
    }


def _configure(monkeypatch, tmp_path, pair: dict, duplicate: dict) -> None:
    expectations = tmp_path / "expectations.json"
    _write_expectations(expectations, [pair])
    monkeypatch.setattr(gate, "EXPECTATIONS_PATH", expectations)
    _stub_report(monkeypatch, duplicate)
    monkeypatch.setattr(
        sys,
        "argv",
        ["gate", "app", "--min-lines", "5", "--min-tokens", "50", "--threshold", "0", "--fail-on-empty"],
    )


def test_app_profile_rejects_an_undeclared_pair_below_threshold(monkeypatch, tmp_path):
    _write_expectations(tmp_path / "expectations.json", [])
    monkeypatch.setattr(gate, "EXPECTATIONS_PATH", tmp_path / "expectations.json")
    _stub_report(monkeypatch, _duplicate())
    assert gate._run(_args(threshold=100)) == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [("reason", " "), ("guard", "tests/test_safety_invariants.py::test_does_not_exist")],
)
def test_expectations_reject_unusable_attestations(monkeypatch, tmp_path, field, value):
    pair = _valid_pair()
    pair[field] = value
    _configure(monkeypatch, tmp_path, pair, _duplicate())
    assert gate.main() == 1


def test_expectation_rejects_changed_fragment(monkeypatch, tmp_path):
    duplicate = _duplicate()
    duplicate["fragment"] = "changed"
    _configure(monkeypatch, tmp_path, _valid_pair(), duplicate)
    assert gate.main() == 1
