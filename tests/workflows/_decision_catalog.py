"""Decision registrations and their assertions, owned outside pytest collection."""

from __future__ import annotations

from tests.workflows._model import PermissionLevel, PermissionScope, workflow
from tests.workflows._registry import Decision, decision


@decision("nightly", Decision.FULL_MARKER_SET, jobs=("test",))
def test_nightly_runs_the_full_marker_set() -> None:
    step = workflow("nightly").jobs["test"].step_named("Run full test suite (no -m filter) with coverage")
    assert "-m" not in (step.run or "").replace("-m pytest", "")


@decision("release", Decision.OIDC_AT_PUBLISH_ONLY, jobs=("publish",))
def test_only_the_publish_job_holds_the_publish_credential() -> None:
    release = workflow("release")
    publish = release.jobs["publish"]
    assert publish.permissions.get(PermissionScope.ID_TOKEN) == PermissionLevel.WRITE
    assert release.jobs["image"].permissions.get(PermissionScope.ID_TOKEN) == PermissionLevel.WRITE
    assert release.jobs["ci"].permissions.get(PermissionScope.ID_TOKEN) == PermissionLevel.WRITE
    for name, job in release.jobs.items():
        if name not in {"ci", "publish", "image"}:
            assert PermissionScope.ID_TOKEN not in job.permissions


@decision("ci", Decision.COVERAGE_NO_RERUN, jobs=("coverage",))
def test_the_coverage_job_does_not_rerun_pytest() -> None:
    job = workflow("ci").jobs["coverage"]
    assert all("pytest" not in (step.run or "") for step in job.steps)


@decision("rehearsal", Decision.NO_WRITE_SCOPES, jobs=())
def test_the_rehearsal_workflow_has_no_write_scope() -> None:
    item = workflow("rehearsal")
    assert all(level != PermissionLevel.WRITE for level in item.permissions.values())
    assert all(
        level != PermissionLevel.WRITE for job in item.jobs.values() for level in job.permissions.values()
    )


@decision("released", Decision.ROSTER_OUTPUTS, jobs=("released",))
def test_the_roster_resolver_exposes_its_three_outputs() -> None:
    called = workflow("released").on_["workflow_call"]
    assert isinstance(called, dict)
    outputs = called["outputs"]
    assert isinstance(outputs, dict)
    assert set(outputs) == {"packages", "root_version", "primary"}


@decision("packaging", Decision.REUSABLE_ONLY, jobs=())
def test_packaging_cannot_publish() -> None:
    item = workflow("packaging")
    assert set(item.on_) == {"workflow_call"}
    assert all("pypi-publish" not in (step.uses or "") for job in item.jobs.values() for step in job.steps)


@decision("release-please", Decision.DISPATCH_ONLY, jobs=())
def test_release_please_is_owner_dispatched() -> None:
    assert set(workflow("release-please").on_) == {"workflow_dispatch"}


@decision("agent-review", Decision.TWO_PLANE_TRUST, jobs=("probes",))
def test_the_probe_plane_holds_no_trusted_credential() -> None:
    probes = workflow("agent-review").jobs["probes"]
    assert all(name not in step.env for step in probes.steps for name in ("NVIDIA_API_KEY", "GITHUB_TOKEN"))


@decision("codeql", Decision.SECURITY_EVENTS_WRITE, jobs=("analyze",))
def test_codeql_uploads_findings_with_the_narrow_write_scope() -> None:
    analyze = workflow("codeql").jobs["analyze"]
    assert analyze.permissions.get(PermissionScope.SECURITY_EVENTS) == PermissionLevel.WRITE


@decision("scorecard", Decision.SCHEDULED_ANALYSIS, jobs=("analysis",))
def test_scorecard_runs_on_a_schedule_and_writes_findings() -> None:
    item = workflow("scorecard")
    assert "schedule" in item.on_
    assert item.jobs["analysis"].permissions.get(PermissionScope.SECURITY_EVENTS) == PermissionLevel.WRITE


@decision("weekly", Decision.MUTATION_TIMEBOX, jobs=("mutation-testing",))
def test_weekly_mutation_job_is_timeboxed() -> None:
    job = workflow("weekly").jobs["mutation-testing"]
    assert job.timeout_minutes is not None and job.timeout_minutes <= 120
