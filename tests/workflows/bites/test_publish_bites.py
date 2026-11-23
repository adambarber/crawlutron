"""Bypasses of the publish-cache-restore rule are named, not just the direct case."""

from __future__ import annotations

from tests.workflows._model import Job, Step, Workflow
from tests.workflows._policy import Finding, RuleId
from tests.workflows._rules import publish_no_cache_restore

_PIN = "a" * 40


def _workflow(jobs: tuple[Job, ...]) -> Workflow:
    return Workflow.create(
        {
            "stem": "release",
            "path": ".github/workflows/release.yml",
            "name": "release",
            "on": {"push": {}},
            "jobs": {job.key: job for job in jobs},
        }
    )


def _publisher(*needs: str) -> Job:
    return Job.create(
        {
            "key": "publish",
            "workflow": "release",
            "line": 100,
            "needs": list(needs),
            "steps": [
                {"name": "Publish", "uses": f"pypa/gh-action-pypi-publish@{_PIN}", "line": 101},
            ],
        }
    )


def _job(key: str, line: int, steps: tuple[Step, ...], *needs: str) -> Job:
    return Job.create(
        {
            "key": key,
            "workflow": "release",
            "line": line,
            "needs": list(needs),
            "steps": list(steps),
        }
    )


def _assert_named(findings: list[Finding], line: int) -> None:
    assert findings
    assert findings[0].rule == RuleId.PUBLISH_NO_CACHE_RESTORE.value
    assert "release.yml" in findings[0].message
    assert str(line) in findings[0].message


def test_two_hop_publish_ancestor_cache_restore_is_named() -> None:
    """A cache restore two needs-edges upstream of a publisher is named."""
    restore = Step.create({"name": "Poison", "uses": f"actions/cache@{_PIN}", "line": 222})
    prep = _job("dev-build-prep", 220, (restore,))
    upload = Step.create({"name": "Stage", "uses": f"actions/upload-artifact@{_PIN}", "line": 202})
    builder = _job("testpypi-build", 200, (upload,), "dev-build-prep")
    wf = _workflow((_publisher("testpypi-build"), builder, prep))
    _assert_named(list(publish_no_cache_restore(wf, prep)), restore.line)


def test_cache_restore_action_is_recognized() -> None:
    """The dedicated actions/cache/restore action restores a cache."""
    restore = Step.create({"name": "Poison", "uses": f"actions/cache/restore@{_PIN}", "line": 302})
    prep = _job("artifact-prep", 300, (restore,))
    wf = _workflow((_publisher("artifact-prep"), prep))
    _assert_named(list(publish_no_cache_restore(wf, prep)), restore.line)


def test_setup_uv_without_enable_cache_defaults_to_restoring() -> None:
    """setup-uv with enable-cache omitted restores a cache by default."""
    restore = Step.create({"name": "Setup uv", "uses": f"astral-sh/setup-uv@{_PIN}", "line": 402})
    prep = _job("artifact-prep", 400, (restore,))
    wf = _workflow((_publisher("artifact-prep"), prep))
    _assert_named(list(publish_no_cache_restore(wf, prep)), restore.line)
