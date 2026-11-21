"""Contract tests for ProbeResultRecord and related probes."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.executables import require_executable

SERVICE_ROOT = Path(__file__).resolve().parents[2]
AGENT_REVIEW_SCRIPT = SERVICE_ROOT / "scripts" / "agent_review.py"
REVIEW_PROBES_SCRIPT = SERVICE_ROOT / "scripts" / "review_probes.py"


def _mapping(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    result: dict[str, object] = {}
    for key, item in value.items():
        assert isinstance(key, str)
        result[key] = item
    return result


def _probe_artifacts(probe_dir: Path) -> None:
    probe_dir.mkdir(parents=True, exist_ok=True)
    for name in ("structural-diff", "ci-gates", "invariants"):
        stdout_path = probe_dir / f"{name}.stdout.txt"
        stderr_path = probe_dir / f"{name}.stderr.txt"
        stdout_path.write_text(f"{name} stdout\n", encoding="utf-8")
        stderr_path.write_text(f"{name} stderr\n", encoding="utf-8")
        payload: dict[str, object] = {
            "name": name,
            "result": {
                "name": name,
                "argv": ["probe", name],
                "exit_code": 0,
                "duration_ms": 1,
                "stdout_path": stdout_path.name,
                "stderr_path": stderr_path.name,
                "stdout_sha256": hashlib.sha256(stdout_path.read_bytes()).hexdigest(),
                "stderr_sha256": hashlib.sha256(stderr_path.read_bytes()).hexdigest(),
            },
            "data": {"changed_files": []} if name == "structural-diff" else {},
        }
        (probe_dir / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")


def test_probe_records_are_read_only_through_shared_accessors() -> None:
    import ast

    tree = ast.parse(REVIEW_PROBES_SCRIPT.read_text(encoding="utf-8"))
    direct: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Subscript):
            continue
        key = node.slice
        if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
            continue
        base = node.value
        if isinstance(base, ast.Name) and base.id == "result":
            direct.append(node.lineno)
        elif isinstance(base, ast.Attribute) and base.attr == "result":
            direct.append(node.lineno)
    assert direct == [], f"direct probe record reads at lines {direct}"


def test_probe_result_record_has_exactly_eight_keys() -> None:
    import typing

    from scripts.review_support import ProbeResultRecord

    hints = typing.get_type_hints(ProbeResultRecord)
    assert set(hints.keys()) == {
        "name",
        "argv",
        "exit_code",
        "duration_ms",
        "stdout_path",
        "stderr_path",
        "stdout_sha256",
        "stderr_sha256",
    }


def test_probe_result_record_accessors_return_correct_types() -> None:
    from scripts.review_support import (
        ProbeResultRecord,
        probe_argv,
        probe_duration_ms,
        probe_exit_code,
        probe_name,
        probe_stderr_path,
        probe_stderr_sha256,
        probe_stdout_path,
        probe_stdout_sha256,
    )

    record: ProbeResultRecord = {
        "name": "test-probe",
        "argv": ["probe", "test-probe"],
        "exit_code": 0,
        "duration_ms": 42,
        "stdout_path": "test.stdout.txt",
        "stderr_path": "test.stderr.txt",
        "stdout_sha256": "a" * 64,
        "stderr_sha256": "b" * 64,
    }
    assert probe_name(record) == "test-probe"
    assert probe_argv(record) == ["probe", "test-probe"]
    assert probe_exit_code(record) == 0
    assert probe_duration_ms(record) == 42
    assert probe_stdout_path(record) == "test.stdout.txt"
    assert probe_stderr_path(record) == "test.stderr.txt"
    assert probe_stdout_sha256(record) == "a" * 64
    assert probe_stderr_sha256(record) == "b" * 64


def test_probe_result_record_accessors_validate_input() -> None:
    from scripts.review_support import as_probe_result_record

    incomplete = {"name": "test", "argv": []}
    try:
        as_probe_result_record(incomplete, "test")
    except ValueError:
        pass
    else:
        raise AssertionError("Expected ValueError for missing keys")

    bad_type = {
        "name": "test",
        "argv": [],
        "exit_code": "not-an-int",
        "duration_ms": 0,
        "stdout_path": "x",
        "stderr_path": "x",
        "stdout_sha256": "a" * 64,
        "stderr_sha256": "b" * 64,
    }
    try:
        as_probe_result_record(bad_type, "test")
    except TypeError, ValueError:
        pass
    else:
        raise AssertionError("Expected TypeError or ValueError for wrong type")


def test_validate_probe_artifact_enforces_exact_eight_keys() -> None:
    from scripts.review_support import validate_probe_artifact

    valid_artifact = {
        "name": "test",
        "result": {
            "name": "test",
            "argv": [],
            "exit_code": 0,
            "duration_ms": 0,
            "stdout_path": "x.stdout.txt",
            "stderr_path": "x.stderr.txt",
            "stdout_sha256": "a" * 64,
            "stderr_sha256": "b" * 64,
        },
        "data": {},
    }
    assert validate_probe_artifact("test", valid_artifact) == valid_artifact

    missing_key = dict(valid_artifact)
    missing_key["result"] = {
        "name": "test",
        "argv": [],
        "exit_code": 0,
        "stdout_path": "x.stdout.txt",
        "stderr_path": "x.stderr.txt",
        "stdout_sha256": "a" * 64,
        "stderr_sha256": "b" * 64,
    }
    try:
        validate_probe_artifact("test", missing_key)
    except ValueError as e:
        assert "eight probe result fields" in str(e)
    else:
        raise AssertionError("Expected ValueError for missing key")

    extra_key = dict(valid_artifact)
    extra_result: dict[str, object] = _mapping(valid_artifact["result"])
    extra_result["extra_field"] = "not-allowed"
    extra_key["result"] = extra_result
    try:
        validate_probe_artifact("test", extra_key)
    except ValueError as e:
        assert "eight probe result fields" in str(e)
    else:
        raise AssertionError("Expected ValueError for extra key")


def test_structural_probe_records_diff_and_output_hashes(tmp_path: Path) -> None:
    from scripts.review_probes import probe_structural_diff
    from scripts.review_support import probe_exit_code, probe_stdout_sha256

    head = subprocess.run(
        [require_executable("git"), "rev-parse", "HEAD"],
        cwd=str(SERVICE_ROOT),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    base = subprocess.run(
        [require_executable("git"), "rev-parse", "HEAD^"],
        cwd=str(SERVICE_ROOT),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    output_path = tmp_path / "structural-diff.json"
    result = probe_structural_diff(base, head, output_path)
    assert result.name == "structural-diff"
    assert probe_exit_code(result.result) == 0
    assert probe_stdout_sha256(result.result)
    artifact = _mapping(json.loads(output_path.read_text(encoding="utf-8")))
    data = _mapping(artifact.get("data"))
    assert data.get("base_sha") == base
    assert data.get("head_sha") == head
    assert isinstance(data.get("diff"), str)


def test_probe_output_path_traversal_is_refused(tmp_path: Path) -> None:
    from scripts.agent_review import _load_probe_artifacts

    probe_dir = tmp_path / "probes"
    _probe_artifacts(probe_dir)
    artifact_path = probe_dir / "invariants.json"
    payload = _mapping(json.loads(artifact_path.read_text(encoding="utf-8")))
    result = _mapping(payload.get("result"))
    result["stdout_path"] = "../outside.txt"
    payload["result"] = result
    artifact_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="escapes"):
        _load_probe_artifacts(probe_dir, ("invariants",))


def test_probe_output_digest_mismatch_is_refused(tmp_path: Path) -> None:
    from scripts.agent_review import _load_probe_artifacts

    probe_dir = tmp_path / "probes"
    _probe_artifacts(probe_dir)
    (probe_dir / "invariants.stdout.txt").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(ValueError, match="digest mismatch"):
        _load_probe_artifacts(probe_dir, ("invariants",))


def test_agent_review_scripts_expose_their_cli() -> None:
    for script, commands in (
        (AGENT_REVIEW_SCRIPT, ("review", "publish-check", "post-review")),
        (REVIEW_PROBES_SCRIPT, ("run", "structural-diff", "ci-gates", "invariants")),
    ):
        result = subprocess.run(
            [sys.executable, str(script), "--help"],
            capture_output=True,
            text=True,
            cwd=str(SERVICE_ROOT),
            check=False,
        )
        assert result.returncode == 0
        for command in commands:
            assert command in result.stdout
