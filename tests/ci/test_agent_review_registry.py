"""Behavior and policy tests for the agent-review scripts and registry."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from tests.chokepoint_paths import REPO_ROOT

SERVICE_ROOT = REPO_ROOT
REGISTRY_PATH = SERVICE_ROOT / ".github" / "review-agents.toml"
AGENT_REVIEW_SCRIPT = SERVICE_ROOT / "scripts" / "agent_review.py"
REVIEW_PROBES_SCRIPT = SERVICE_ROOT / "scripts" / "review_probes.py"


def _mapping(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    result: dict[str, object] = {}
    for key, item in value.items():
        assert isinstance(key, str)
        result[key] = item
    return result


def _run_review(env: dict[str, str], probe_dir: Path, output_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(AGENT_REVIEW_SCRIPT),
            "review",
            "--pr-number",
            "1",
            "--head-sha",
            "abc123",
            "--probe-dir",
            str(probe_dir),
            "--registry",
            str(REGISTRY_PATH),
            "--output",
            str(output_path),
        ],
        capture_output=True,
        text=True,
        cwd=str(SERVICE_ROOT),
        env=env,
        check=False,
    )


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


def _write_verdict(path: Path, verdict: str) -> None:
    path.write_text(
        json.dumps({"verdict": verdict, "findings": [], "summary": "review summary"}), encoding="utf-8"
    )


def _current_head_get(
    url: str, *, headers: dict[str, str], timeout: float, follow_redirects: bool
) -> httpx.Response:
    return httpx.Response(
        200, json={"state": "open", "head": {"sha": "abc123"}}, request=httpx.Request("GET", url)
    )


def _stale_head_get(
    url: str, *, headers: dict[str, str], timeout: float, follow_redirects: bool
) -> httpx.Response:
    return httpx.Response(
        200, json={"state": "open", "head": {"sha": "new-head"}}, request=httpx.Request("GET", url)
    )


def test_registry_parser_selects_the_configured_default_agent() -> None:
    from scripts.review_registry import load_registry

    registry = load_registry(REGISTRY_PATH)
    assert registry.default_agent == "code-review"
    assert registry.agents[registry.default_agent].probes


def test_registry_parser_rejects_an_unregistered_probe(tmp_path: Path) -> None:
    from scripts.review_registry import load_registry

    registry_path = tmp_path / "review-agents.toml"
    registry_path.write_text(
        """[defaults]
agent = "code-review"
[agents.code-review]
model = "example-model"
prompt = ".github/agents/prompt.md"
probes = ["missing-probe"]
severity_threshold = "P1"
[probes.diff]
command = "structural-diff"
description = "diff"
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unregistered probes"):
        load_registry(registry_path)


def test_registry_parser_rejects_an_unknown_agent_field(tmp_path: Path) -> None:
    from scripts.review_registry import load_registry

    registry_path = tmp_path / "review-agents.toml"
    registry_path.write_text(
        """[defaults]
agent = "code-review"
[agents.code-review]
model = "example-model"
prompt = ".github/agents/prompt.md"
probes = ["diff"]
severity_threshold = "P1"
path = ["**/*.py"]
[probes.diff]
command = "structural-diff"
description = "diff"
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown fields"):
        load_registry(registry_path)


def test_registry_parser_rejects_an_unknown_root_field(tmp_path: Path) -> None:
    from scripts.review_registry import load_registry

    registry_path = tmp_path / "review-agents.toml"
    registry_path.write_text(
        """defualts = "typo"
[defaults]
agent = "code-review"
[agents.code-review]
model = "example-model"
prompt = ".github/agents/prompt.md"
probes = ["diff"]
severity_threshold = "P1"
[probes.diff]
command = "structural-diff"
description = "diff"
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown fields"):
        load_registry(registry_path)


def test_registry_parser_rejects_an_unregistered_default_agent(tmp_path: Path) -> None:
    from scripts.review_registry import load_registry

    registry_path = tmp_path / "review-agents.toml"
    registry_path.write_text(
        """[defaults]
agent = "missing"
[agents.code-review]
model = "example-model"
prompt = ".github/agents/prompt.md"
probes = ["diff"]
severity_threshold = "P1"
[probes.diff]
command = "structural-diff"
description = "diff"
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="default agent"):
        load_registry(registry_path)


def test_registry_parser_honors_path_routing(tmp_path: Path) -> None:
    from scripts.review_registry import load_registry, select_agent

    registry_path = tmp_path / "review-agents.toml"
    registry_path.write_text(
        """[defaults]
agent = "general"
[agents.general]
model = "general-model"
prompt = ".github/agents/general.md"
probes = ["diff"]
severity_threshold = "P1"
[agents.special]
model = "special-model"
prompt = ".github/agents/special.md"
probes = ["diff"]
severity_threshold = "P1"
[probes.diff]
command = "structural-diff"
description = "diff"
[routing]
path_rules = [{ pattern = "packages/mcp/**", agent = "special" }]
label_rules = []
""",
        encoding="utf-8",
    )
    registry = load_registry(registry_path)
    selected = select_agent(registry, changed_files=("packages/mcp/server.py",), labels=())
    assert selected.model == "special-model"


def test_agent_review_fails_closed_without_nvidia_key(tmp_path: Path) -> None:
    env = {**os.environ}
    env.pop("NVIDIA_API_KEY", None)
    env.pop("GITHUB_TOKEN", None)
    output_path = tmp_path / "verdict.json"
    result = _run_review(env, tmp_path / "probes", output_path)
    assert result.returncode != 0
    assert "NVIDIA_API_KEY" in result.stderr
    assert output_path.exists()
    verdict = _mapping(json.loads(output_path.read_text(encoding="utf-8")))
    assert verdict.get("verdict") == "REQUEST_CHANGES"


def test_agent_review_fails_closed_without_app_token(tmp_path: Path) -> None:
    env = {**os.environ}
    env["NVIDIA_API_KEY"] = "test-key"
    env.pop("GITHUB_TOKEN", None)
    output_path = tmp_path / "verdict.json"
    result = _run_review(env, tmp_path / "probes", output_path)
    assert result.returncode != 0
    assert "GITHUB_TOKEN" in result.stderr
    assert output_path.exists()


def test_agent_review_fails_closed_on_missing_probe_data(tmp_path: Path) -> None:
    env = {**os.environ}
    env["NVIDIA_API_KEY"] = "test-key"
    env["GITHUB_TOKEN"] = "test-token"
    probe_dir = tmp_path / "probes"
    probe_dir.mkdir()
    output_path = tmp_path / "verdict.json"
    result = _run_review(env, probe_dir, output_path)
    assert result.returncode != 0
    assert "probe" in result.stderr.lower()
    assert output_path.exists()


def test_agent_review_fails_closed_on_a_failed_probe(tmp_path: Path) -> None:
    env = {**os.environ}
    env["NVIDIA_API_KEY"] = "test-key"
    env["GITHUB_TOKEN"] = "test-token"
    probe_dir = tmp_path / "probes"
    _probe_artifacts(probe_dir)
    artifact_path = probe_dir / "invariants.json"
    payload = _mapping(json.loads(artifact_path.read_text(encoding="utf-8")))
    result_payload = _mapping(payload.get("result"))
    result_payload["exit_code"] = 1
    payload["result"] = result_payload
    artifact_path.write_text(json.dumps(payload), encoding="utf-8")
    output_path = tmp_path / "verdict.json"
    result = _run_review(env, probe_dir, output_path)
    assert result.returncode != 0
    assert "probe" in result.stderr.lower()
    verdict = _mapping(json.loads(output_path.read_text(encoding="utf-8")))
    assert verdict.get("verdict") == "REQUEST_CHANGES"


def test_validate_verdict_rejects_unknown_and_malformed_fields() -> None:
    from scripts.agent_review import validate_verdict

    valid = validate_verdict(
        '{"verdict":"REQUEST_CHANGES","findings":[],"summary":"A source-grounded failure"}'
    )
    assert valid.verdict == "REQUEST_CHANGES"
    with pytest.raises(ValueError, match="Missing required field: verdict"):
        validate_verdict('{"findings":[],"summary":"missing"}')
    with pytest.raises(ValueError, match="[Ii]nvalid verdict value"):
        validate_verdict('{"verdict":"INVALID","findings":[],"summary":"bad"}')
    with pytest.raises(ValueError, match="missing required field"):
        validate_verdict('{"verdict":"APPROVE","findings":[{"file":"x.py"}],"summary":"bad"}')
    with pytest.raises(ValueError, match="unknown fields"):
        validate_verdict('{"verdict":"APPROVE","findings":[],"summary":"bad","extra":true}')
    with pytest.raises(ValueError, match="repository-relative"):
        validate_verdict(
            '{"verdict":"REQUEST_CHANGES","findings":[{"file":"../../etc/passwd","line":1,"severity":"P1","rule":"r","rationale":"x","confidence":"High"}],"summary":"bad"}'
        )


def test_nvidia_call_uses_the_configured_model_and_fixed_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.agent_review import call_nvidia_api

    calls: list[dict[str, object]] = []

    def fake_post(
        url: str, *, json: object, headers: dict[str, str], timeout: float, follow_redirects: bool
    ) -> httpx.Response:
        calls.append({"url": url, "json": json, "headers": headers})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "verdict text"}}]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    assert call_nvidia_api("secret", "prompt", "configured-model") == "verdict text"
    assert calls[0]["url"] == "https://integrate.api.nvidia.com/v1/chat/completions"
    payload = _mapping(calls[0]["json"])
    assert payload.get("model") == "configured-model"
    headers = _mapping(calls[0]["headers"])
    assert headers.get("Authorization") == "Bearer secret"


def test_stale_pull_request_head_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.agent_review import ensure_current_head

    monkeypatch.setattr(httpx, "get", _stale_head_get)
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repository")
    with pytest.raises(ValueError, match="stale"):
        ensure_current_head(1, "old-head", "test-token")


def test_stale_head_blocks_check_publication(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.agent_review import publish_check

    calls: list[dict[str, object]] = []

    def fake_post(
        url: str, *, json: object, headers: dict[str, str], timeout: float, follow_redirects: bool
    ) -> httpx.Response:
        calls.append({"url": url, "json": json, "headers": headers})
        return httpx.Response(201, json={}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(httpx, "get", _stale_head_get)
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repository")
    verdict_path = tmp_path / "verdict.json"
    _write_verdict(verdict_path, "APPROVE")
    assert publish_check(verdict_path, "test-token", 1, "abc123") == 1
    assert calls == []


@pytest.mark.parametrize(
    ("verdict", "expected_conclusion"), [("APPROVE", "success"), ("REQUEST_CHANGES", "failure")]
)
def test_check_run_is_published_for_both_verdicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verdict: str, expected_conclusion: str
) -> None:
    from scripts.agent_review import publish_check

    calls: list[dict[str, object]] = []

    def fake_post(
        url: str, *, json: object, headers: dict[str, str], timeout: float, follow_redirects: bool
    ) -> httpx.Response:
        calls.append({"url": url, "json": json, "headers": headers})
        return httpx.Response(
            201, json={"html_url": "https://example.test/check"}, request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(httpx, "get", _current_head_get)
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repository")
    verdict_path = tmp_path / "verdict.json"
    _write_verdict(verdict_path, verdict)
    assert publish_check(verdict_path, "test-token", 1, "abc123") == 0
    payload = _mapping(calls[0]["json"])
    assert payload.get("name") == "code-review"
    assert payload.get("conclusion") == expected_conclusion


@pytest.mark.parametrize("verdict", ["APPROVE", "REQUEST_CHANGES"])
def test_pr_publication_is_always_an_advisory_comment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verdict: str
) -> None:
    from scripts.agent_review import post_review

    calls: list[dict[str, object]] = []

    def fake_post(
        url: str, *, json: object, headers: dict[str, str], timeout: float, follow_redirects: bool
    ) -> httpx.Response:
        calls.append({"url": url, "json": json, "headers": headers})
        return httpx.Response(
            200, json={"html_url": "https://example.test/review"}, request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(httpx, "get", _current_head_get)
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repository")
    verdict_path = tmp_path / "verdict.json"
    _write_verdict(verdict_path, verdict)
    assert post_review(verdict_path, "test-token", 1, "abc123") == 0
    payload = _mapping(calls[0]["json"])
    assert payload.get("event") == "COMMENT"
    assert payload.get("commit_id") == "abc123"
