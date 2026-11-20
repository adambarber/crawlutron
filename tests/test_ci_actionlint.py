"""Actionlint gate tests: the actionlint hook is enforced as a CI job."""

from __future__ import annotations

from tests._ci_contract import REPORTING_JOBS, SERVICE_ROOT, WORKFLOW_PATH, _job_run_steps, hook_run


def test_actionlint_hook_has_a_ci_job() -> None:
    """The actionlint hook must be enforced by a CI job running the hook itself."""
    runs = [run.strip() for _, run in _job_run_steps(WORKFLOW_PATH.read_text(encoding="utf-8"))]
    assert hook_run("actionlint") in runs, "hook actionlint has no CI job running it"


def test_actionlint_in_reporting_jobs() -> None:
    """actionlint must be in REPORTING_JOBS so the required aggregate depends on it."""
    assert "actionlint" in REPORTING_JOBS


def test_d209_ignore_is_removed_from_ruff_config() -> None:
    """The dead D209 ignore must not appear in pyproject.toml's ruff lint config."""
    text = (SERVICE_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    start = text.index("[tool.ruff.lint]")
    end = text.index("\n[", start + 1)
    assert "D209" not in text[start:end], "pyproject.toml ruff lint config still lists D209"


def test_known_first_party_includes_member_packages() -> None:
    """known-first-party must include the three member packages named by the ticket."""
    text = (SERVICE_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    start = text.index("known-first-party")
    end = text.index("]", start)
    for name in ("crawlutron_extract", "crawlutron_mcp", "safe_url"):
        assert name in text[start:end], f"pyproject.toml known-first-party must list {name}"
