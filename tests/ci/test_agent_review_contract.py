"""Contract tests for the agent-review workflow and publication plane."""

from __future__ import annotations

import tomllib

import pytest
import yaml

from tests._ci_contract import SERVICE_ROOT

WORKFLOW_PATH = SERVICE_ROOT / ".github" / "workflows" / "agent-review.yml"
REGISTRY_PATH = SERVICE_ROOT / ".github" / "review-agents.toml"
CODEOWNERS_PATH = SERVICE_ROOT / ".github" / "CODEOWNERS"
ZIZMOR_PATH = SERVICE_ROOT / ".github" / "zizmor.yml"
REVIEW_SCRIPT_PATH = SERVICE_ROOT / "scripts" / "agent_review.py"


def _mapping(value: object) -> dict[str, object]:
    """Narrow a YAML value to a string-keyed mapping."""
    assert isinstance(value, dict)
    result: dict[str, object] = {}
    for key, item in value.items():
        assert isinstance(key, str)
        result[key] = item
    return result


def _steps(job: dict[str, object]) -> list[dict[str, object]]:
    """Return a job's step mappings."""
    steps = job.get("steps")
    assert isinstance(steps, list)
    return [_mapping(step) for step in steps]


def _uses(steps: list[dict[str, object]], fragment: str) -> list[dict[str, object]]:
    """Return steps whose action reference contains a fragment."""
    return [step for step in steps if fragment in str(step.get("uses", ""))]


def _run_text(steps: list[dict[str, object]]) -> str:
    """Join all shell commands in a step list."""
    return "\n".join(str(step.get("run", "")) for step in steps)


def _workflow() -> dict[str, object]:
    """Load the agent-review workflow with YAML 1.1 boolean keys normalized."""
    parsed: object = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    normalized: dict[str, object] = {}
    for key, value in parsed.items():
        name = "on" if key is True else key
        assert isinstance(name, str)
        normalized[name] = value
    return normalized


def _jobs() -> dict[str, dict[str, object]]:
    """Return the workflow's job mappings."""
    jobs = _mapping(_workflow().get("jobs"))
    return {name: _mapping(job) for name, job in jobs.items()}


def _triggers() -> dict[str, object]:
    """Return the workflow trigger mappings."""
    return _mapping(_workflow().get("on"))


def _env_names(value: object) -> set[str]:
    """Collect environment variable names from a workflow value."""
    names: set[str] = set()
    if isinstance(value, dict):
        env = value.get("env")
        if isinstance(env, dict):
            names.update(str(key) for key in env)
        for item in value.values():
            names.update(_env_names(item))
    elif isinstance(value, list):
        for item in value:
            names.update(_env_names(item))
    return names


def test_agent_review_workflow_exists() -> None:
    """The agent-review workflow must exist."""
    assert WORKFLOW_PATH.exists()


def test_agent_review_triggers_only_on_completed_ci() -> None:
    """Only a completed CI workflow may trigger agent review."""
    triggers = _triggers()
    assert set(triggers) == {"workflow_run"}
    workflow_run = _mapping(triggers["workflow_run"])
    assert workflow_run.get("workflows") == ["CI"]
    assert workflow_run.get("types") == ["completed"]
    assert "pull_request" not in triggers
    assert "pull_request_target" not in triggers


def test_agent_review_jobs_gate_on_green_pull_request_ci() -> None:
    """Both planes must require a green pull-request CI run."""
    for name in ("probes", "review"):
        condition = _jobs()[name].get("if")
        assert isinstance(condition, str)
        assert "github.event.workflow_run.event == 'pull_request'" in condition
        assert "github.event.workflow_run.conclusion == 'success'" in condition


def test_probe_plane_is_secretless_and_read_only() -> None:
    """The probe plane must not receive credentials or write scopes."""
    probe_job = _jobs()["probes"]
    assert probe_job.get("permissions") == {"contents": "read"}
    env_names = _env_names(probe_job)
    assert "NVIDIA_API_KEY" not in env_names
    assert "GITHUB_TOKEN" not in env_names
    assert "REVIEW_APP_PRIVATE_KEY" not in env_names


def test_probe_plane_checks_out_base_and_executes_base_scripts() -> None:
    """The probe plane must execute pinned scripts from the base tree."""
    steps = _steps(_jobs()["probes"])
    checkout = _uses(steps, "actions/checkout")
    assert len(checkout) == 1
    checkout_with = _mapping(checkout[0].get("with"))
    assert "ref" not in checkout_with, (
        "Probe checkout must not use a context expression in with.ref; "
        "it checks out the trusted default branch"
    )
    assert checkout_with.get("persist-credentials") is False
    run_text = _run_text(steps)
    assert "scripts/review_probes.py run" in run_text
    assert "--registry .github/review-agents.toml" in run_text
    assert "scripts/review_probes.py structural-diff" not in run_text


def test_probe_plane_fetches_head_as_detached_read_only_worktree() -> None:
    """The PR head must be fetched without becoming executable probe code."""
    steps = _steps(_jobs()["probes"])
    run_text = _run_text(steps)
    assert "refs/pull/" in run_text
    assert "git worktree add --detach" in run_text
    assert "chmod -R a-w" in run_text
    for step in steps:
        run = str(step.get("run", ""))
        assert "${{" not in run


def test_probe_plane_uploads_the_structured_artifact() -> None:
    """The probe plane must upload its structured artifact."""
    steps = _steps(_jobs()["probes"])
    uploads = _uses(steps, "actions/upload-artifact")
    assert uploads
    upload_with = _mapping(uploads[0].get("with"))
    assert upload_with.get("name") == "agent-review-probes"
    assert upload_with.get("path") == "probes/"


def test_review_plane_never_checks_out_pull_request_code() -> None:
    """The trusted review plane must execute only trusted repository code."""
    steps = _steps(_jobs()["review"])
    checkout = _uses(steps, "actions/checkout")
    assert len(checkout) == 1
    checkout_with = _mapping(checkout[0].get("with"))
    assert "ref" not in checkout_with, (
        "Review checkout must not use a context expression in with.ref; "
        "it checks out the trusted default branch"
    )
    assert checkout_with.get("persist-credentials") is False


def test_review_plane_uses_app_token_and_nvidia_secret() -> None:
    """The trusted plane must use least-privilege App and NVIDIA credentials."""
    review_job = _jobs()["review"]
    assert review_job.get("permissions") == {
        "contents": "read",
        "checks": "write",
        "pull-requests": "write",
    }
    steps = _steps(review_job)
    app_steps = _uses(steps, "actions/create-github-app-token")
    assert app_steps
    app_with = _mapping(app_steps[0].get("with"))
    assert app_with.get("app-id") == "${{ vars.REVIEW_APP_ID }}"
    assert app_with.get("private-key") == "${{ secrets.REVIEW_APP_PRIVATE_KEY }}"
    env_names = _env_names(review_job)
    assert "NVIDIA_API_KEY" in env_names
    assert "GITHUB_TOKEN" in env_names


def test_review_plane_uses_only_the_app_token_for_github_writes() -> None:
    """The trusted plane must fail closed instead of falling back to GITHUB_TOKEN."""
    steps = _steps(_jobs()["review"])
    credential_steps = [
        step
        for step in steps
        if isinstance(step.get("env"), dict) and "GITHUB_TOKEN" in _mapping(step["env"])
    ]
    assert credential_steps
    for step in credential_steps:
        env = _mapping(step["env"])
        assert env["GITHUB_TOKEN"] == "${{ steps.app-token.outputs.token }}"
        assert "github.token" not in str(step.get("run", ""))
    guard_steps = [step for step in steps if step.get("name") == "Require GitHub App credentials"]
    assert len(guard_steps) == 1
    assert "exit 1" in str(guard_steps[0].get("run", ""))


def test_review_plane_consumes_and_publishes_required_artifacts() -> None:
    """The trusted plane must consume probes and publish both outputs."""
    steps = _steps(_jobs()["review"])
    downloads = _uses(steps, "actions/download-artifact")
    uploads = _uses(steps, "actions/upload-artifact")
    assert downloads
    assert uploads
    download_with = _mapping(downloads[0].get("with"))
    assert download_with.get("name") == "agent-review-probes"
    upload_names = {_mapping(step.get("with")).get("name") for step in uploads}
    assert "agent-review-verdict" in upload_names
    run_text = _run_text(steps)
    assert "scripts/agent_review.py review" in run_text
    assert "scripts/agent_review.py publish-check" in run_text
    assert "scripts/agent_review.py post-review" in run_text


def test_pr_publication_is_advisory_and_never_approves_or_merges() -> None:
    """The bot may comment, but it must never approve or merge."""
    source = REVIEW_SCRIPT_PATH.read_text(encoding="utf-8")
    assert '"event": "COMMENT"' in source
    assert '"event": "APPROVE"' not in source
    assert "/merge" not in source
    assert "pulls/{pr_number}/reviews" in source


def test_model_and_prompt_policy_exist_only_in_registry() -> None:
    """Model IDs and prompt paths must have one registry home."""
    data = tomllib.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    agents = _mapping(data.get("agents"))
    default_agent = str(_mapping(data.get("defaults")).get("agent"))
    agent = _mapping(agents[default_agent])
    policy_values = (str(agent.get("model")), str(agent.get("prompt")))
    search_roots = (SERVICE_ROOT / ".github", SERVICE_ROOT / "scripts", SERVICE_ROOT / "tests")
    for root in search_roots:
        for path in root.rglob("*"):
            if not path.is_file() or path == REGISTRY_PATH:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for value in policy_values:
                assert value not in text, f"{value!r} escaped the registry in {path}"


def test_agent_review_workflow_has_owner_and_zizmor_coverage() -> None:
    """The new workflow must have an owner and explicit audit policy."""
    codeowners = CODEOWNERS_PATH.read_text(encoding="utf-8")
    zizmor = ZIZMOR_PATH.read_text(encoding="utf-8")
    assert ".github/workflows/agent-review.yml @adambarber" in codeowners
    assert ".github/review-agents.toml @adambarber" in codeowners
    assert "agent-review.yml" in zizmor


@pytest.mark.parametrize("secret_name", ["NVIDIA_API_KEY", "GITHUB_TOKEN", "REVIEW_APP_PRIVATE_KEY"])
def test_probe_steps_never_receive_trusted_plane_secrets(secret_name: str) -> None:
    """No probe step may inherit a trusted-plane secret."""
    assert secret_name not in _env_names(_jobs()["probes"])


def test_workflow_run_checkout_no_context_ref() -> None:
    """No workflow_run job's actions/checkout step uses a context expression in with.ref."""
    triggers = _triggers()
    assert "workflow_run" in triggers, "workflow_run trigger not found"

    jobs = _jobs()
    for job_name, job in jobs.items():
        steps = _steps(job)
        checkouts = _uses(steps, "actions/checkout")
        for checkout in checkouts:
            checkout_with = _mapping(checkout.get("with", {}))
            ref = checkout_with.get("ref", "")
            assert isinstance(ref, str), f"Expected ref to be a string, got {type(ref)}"
            assert "${{" not in ref, f"Job '{job_name}' has context expression in ref: {ref}"
