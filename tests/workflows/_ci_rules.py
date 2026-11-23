"""Typed rules for the ci.yml history/requirements contract."""

from __future__ import annotations

from collections.abc import Iterator

from tests._ci_contract import REPORTING_JOBS
from tests.workflows._model import Job, Step, Workflow
from tests.workflows._policy import Finding, RuleId


def _checkout_steps(job: Job) -> list[Step]:
    return [step for step in job.steps if step.uses and step.uses.startswith("actions/checkout@")]


def _fetch_depth(step: Step) -> str | None:
    return str(step.with_.get("fetch-depth", ""))


def ci_secret_scan_fetch_depth(workflow: Workflow, job: Job) -> Iterator[Finding]:
    if workflow.stem != "ci" or job.key != "secret-scan":
        return
    checkouts = _checkout_steps(job)
    if len(checkouts) != 1:
        yield Finding(
            RuleId.CI_SECRET_SCAN_FETCH_DEPTH.value,
            workflow.stem,
            job.key,
            job.line,
            f"{workflow.path}:{job.line}: secret-scan must check out once",
        )
        return
    if _fetch_depth(checkouts[0]) != "0":
        yield Finding(
            RuleId.CI_SECRET_SCAN_FETCH_DEPTH.value,
            workflow.stem,
            job.key,
            checkouts[0].line,
            f"{workflow.path}:{checkouts[0].line}: secret-scan checkout must use fetch-depth 0",
        )


def ci_dependency_review_checkout(workflow: Workflow, job: Job) -> Iterator[Finding]:
    if workflow.stem != "ci" or job.key != "dependency-review":
        return
    checkouts = _checkout_steps(job)
    if len(checkouts) != 1:
        yield Finding(
            RuleId.CI_DEPENDENCY_REVIEW_CHECKOUT.value,
            workflow.stem,
            job.key,
            job.line,
            f"{workflow.path}:{job.line}: dependency-review must check out once",
        )


def ci_dependency_review_refs(workflow: Workflow, job: Job) -> Iterator[Finding]:
    if workflow.stem != "ci" or job.key != "dependency-review":
        return
    for step in job.steps:
        uses = step.uses or ""
        if "dependency-review-action@" in uses:
            base_ref = str(step.with_.get("base-ref", ""))
            head_ref = str(step.with_.get("head-ref", ""))
            if "github.event.before" not in base_ref:
                yield Finding(
                    RuleId.CI_DEPENDENCY_REVIEW_REFS.value,
                    workflow.stem,
                    job.key,
                    step.line,
                    f"{workflow.path}:{step.line}: dependency-review base-ref must cover github.event.before",
                )
            if "github.sha" not in head_ref:
                yield Finding(
                    RuleId.CI_DEPENDENCY_REVIEW_REFS.value,
                    workflow.stem,
                    job.key,
                    step.line,
                    f"{workflow.path}:{step.line}: dependency-review head-ref must cover github.sha",
                )
            return
    yield Finding(
        RuleId.CI_DEPENDENCY_REVIEW_REFS.value,
        workflow.stem,
        job.key,
        job.line,
        f"{workflow.path}:{job.line}: dependency-review step not found",
    )


def ci_real_pbkdf2_door(workflow: Workflow, job: Job) -> Iterator[Finding]:
    if workflow.stem != "ci":
        return
    if job.key == "real-pbkdf2":
        if job.if_ is not None:
            yield Finding(
                RuleId.CI_REAL_PBKDF2_DOOR.value,
                workflow.stem,
                job.key,
                job.line,
                f"{workflow.path}:{job.line}: real-pbkdf2 job must not carry a job-level if",
            )
        door_steps = [step for step in job.steps if step.uses and "run-tests" in step.uses]
        if len(door_steps) != 1:
            yield Finding(
                RuleId.CI_REAL_PBKDF2_DOOR.value,
                workflow.stem,
                job.key,
                job.line,
                f"{workflow.path}:{job.line}: real-pbkdf2 job must call the run-tests door exactly once",
            )
            return
        door = door_steps[0]
        if door.if_ is not None:
            yield Finding(
                RuleId.CI_REAL_PBKDF2_DOOR.value,
                workflow.stem,
                job.key,
                door.line,
                f"{workflow.path}:{door.line}: real-pbkdf2 door step must not carry a step-level if",
            )
        if str(door.with_.get("marker", "")) != "real_pbkdf2":
            yield Finding(
                RuleId.CI_REAL_PBKDF2_DOOR.value,
                workflow.stem,
                job.key,
                door.line,
                f"{workflow.path}:{door.line}: real-pbkdf2 door must pass marker real_pbkdf2",
            )
    if job.key == "coverage":
        for step in job.steps:
            name = str(step.name or "")
            if name.startswith("Download real-pbkdf2") or name.startswith("Upload real-pbkdf2"):
                guard = str(step.if_ or "")
                if "needs.changes.outputs.real-pbkdf2 == 'true'" not in guard:
                    yield Finding(
                        RuleId.CI_REAL_PBKDF2_DOOR.value,
                        workflow.stem,
                        job.key,
                        step.line,
                        f"{workflow.path}:{step.line}: coverage step {name!r} lost the real-pbkdf2 guard",
                    )


def ci_actionlint_hook(workflow: Workflow, job: Job) -> Iterator[Finding]:
    if workflow.stem != "ci" or job.key != "actionlint":
        return
    if not any(
        step.run and ".venv/bin/pre-commit run actionlint --all-files" in step.run for step in job.steps
    ):
        yield Finding(
            RuleId.CI_ACTIONLINT_HOOK.value,
            workflow.stem,
            job.key,
            job.line,
            f"{workflow.path}:{job.line}: actionlint job must run the actionlint hook itself",
        )


def ci_browser_fpgen_model(workflow: Workflow, job: Job) -> Iterator[Finding]:
    if workflow.stem != "ci" or job.key != "test-browser":
        return
    if not any(step.uses and "run-tests" in step.uses for step in job.steps):
        yield Finding(
            RuleId.CI_BROWSER_FPGEN_MODEL.value,
            workflow.stem,
            job.key,
            job.line,
            f"{workflow.path}:{job.line}: test-browser job must call the run-tests door",
        )


def ci_bytecode_compileall(workflow: Workflow, job: Job) -> Iterator[Finding]:
    if workflow.stem != "ci" or job.key != "battery":
        return
    runs = " ".join(str(step.run or "") for step in job.steps)
    if "compileall" not in runs:
        yield Finding(
            RuleId.CI_BYTECODE_COMPILEALL.value,
            workflow.stem,
            job.key,
            job.line,
            f"{workflow.path}:{job.line}: battery job must precompile bytecode with compileall",
        )
        return
    if "packages/*/src" not in runs:
        yield Finding(
            RuleId.CI_BYTECODE_COMPILEALL.value,
            workflow.stem,
            job.key,
            job.line,
            f"{workflow.path}:{job.line}: compileall must derive scope from member src roots",
        )
    if "tests" not in runs or "scripts" not in runs:
        yield Finding(
            RuleId.CI_BYTECODE_COMPILEALL.value,
            workflow.stem,
            job.key,
            job.line,
            f"{workflow.path}:{job.line}: compileall must cover tests and scripts",
        )


def ci_coverage_needs(workflow: Workflow, job: Job) -> Iterator[Finding]:
    if workflow.stem != "ci" or job.key != "coverage":
        return
    needs = job.needs
    if set(needs) != {"changes", "battery", "real-pbkdf2", "test-browser"}:
        yield Finding(
            RuleId.CI_COVERAGE_NEEDS.value,
            workflow.stem,
            job.key,
            job.line,
            f"{workflow.path}:{job.line}: coverage needs must be changes, battery, real-pbkdf2, test-browser",
        )


def ci_coverage_if_always(workflow: Workflow, job: Job) -> Iterator[Finding]:
    if workflow.stem != "ci" or job.key != "coverage":
        return
    if str(job.if_ or "") != "${{ always() }}":
        yield Finding(
            RuleId.CI_COVERAGE_IF_ALWAYS.value,
            workflow.stem,
            job.key,
            job.line,
            f"{workflow.path}:{job.line}: coverage job must run if: always()",
        )


def ci_coverage_download_artifacts(workflow: Workflow, job: Job) -> Iterator[Finding]:
    if workflow.stem != "ci" or job.key != "coverage":
        return
    names = [
        str(step.with_.get("name", ""))
        for step in job.steps
        if step.uses and "download-artifact" in step.uses
    ]
    for required in (
        "coverage-data",
        "junit-results",
        "coverage-data-real-pbkdf2",
        "coverage-data-test-browser",
    ):
        if required not in names:
            yield Finding(
                RuleId.CI_COVERAGE_DOWNLOAD_ARTIFACTS.value,
                workflow.stem,
                job.key,
                job.line,
                f"{workflow.path}:{job.line}: coverage must download {required} artifact",
            )


def ci_coverage_upload_hidden(workflow: Workflow, job: Job) -> Iterator[Finding]:
    if workflow.stem != "ci" or job.key not in {"battery", "real-pbkdf2", "test-browser"}:
        return
    uploads = [step for step in job.steps if step.uses and "upload-artifact" in step.uses]
    hidden = [s for s in uploads if str(s.with_.get("path", "")) == ".coverage*"]
    if not any(str(s.with_.get("include-hidden-files")) in ("True", "true") for s in hidden):
        yield Finding(
            RuleId.CI_COVERAGE_UPLOAD_HIDDEN.value,
            workflow.stem,
            job.key,
            job.line,
            f"{workflow.path}:{job.line}: {job.key} must upload .coverage* with include-hidden-files",
        )


def ci_coverage_junit_upload(workflow: Workflow, job: Job) -> Iterator[Finding]:
    if workflow.stem != "ci" or job.key not in {"battery", "real-pbkdf2", "test-browser"}:
        return
    junit = [
        step
        for step in job.steps
        if step.uses and "upload-artifact" in step.uses and "junit" in str(step.with_.get("name", "")).lower()
    ]
    if not junit:
        yield Finding(
            RuleId.CI_COVERAGE_JUNIT_UPLOAD.value,
            workflow.stem,
            job.key,
            job.line,
            f"{workflow.path}:{job.line}: {job.key} must upload a junit artifact",
        )


def ci_coverage_distinct_junit_paths(workflow: Workflow, job: Job) -> Iterator[Finding]:
    if workflow.stem != "ci" or job.key != "coverage":
        return
    downloads = [step for step in job.steps if step.uses and "download-artifact" in step.uses]
    junit_downloads = [s for s in downloads if "junit" in str(s.with_.get("name", "")).lower()]
    paths = {str(s.with_.get("path", ".")) for s in junit_downloads}
    if len(paths) != len(junit_downloads):
        yield Finding(
            RuleId.CI_COVERAGE_DISTINCT_JUNIT_PATHS.value,
            workflow.stem,
            job.key,
            job.line,
            f"{workflow.path}:{job.line}: coverage junit downloads must use distinct paths",
        )


def ci_coverage_uploads_coverage_xml(workflow: Workflow, job: Job) -> Iterator[Finding]:
    if workflow.stem != "ci" or job.key != "coverage":
        return
    uploads = [
        step
        for step in job.steps
        if step.uses and "codecov" in str(step.uses) and "coverage.xml" in str(step.with_.get("files", ""))
    ]
    if not uploads:
        yield Finding(
            RuleId.CI_COVERAGE_UPLOADS_COVERAGE_XML.value,
            workflow.stem,
            job.key,
            job.line,
            f"{workflow.path}:{job.line}: coverage job must upload coverage_reports/coverage.xml to Codecov",
        )


def ci_coverage_single_artifact(workflow: Workflow, job: Job) -> Iterator[Finding]:
    if workflow.stem != "ci" or job.key != "coverage":
        return
    artifacts = [step for step in job.steps if step.uses and "upload-artifact" in step.uses]
    if len(artifacts) != 1:
        yield Finding(
            RuleId.CI_COVERAGE_SINGLE_ARTIFACT.value,
            workflow.stem,
            job.key,
            job.line,
            f"{workflow.path}:{job.line}: coverage job must emit exactly one artifact",
        )


def ci_packaging_read_only(workflow: Workflow) -> Iterator[Finding]:
    if workflow.stem != "packaging":
        return
    if dict(workflow.permissions) != {"contents": "read"}:
        yield Finding(
            RuleId.CI_PACKAGING_READ_ONLY.value,
            workflow.stem,
            "",
            1,
            f"{workflow.path}: packaging workflow must declare only contents: read",
        )
    for job in workflow.jobs.values():
        for step in job.steps:
            uses = str(step.uses or "")
            run = str(step.run or "")
            for banned in ("pypi-publish", "twine upload", "docker push", "push: true"):
                if banned in uses or banned in run:
                    yield Finding(
                        RuleId.CI_PACKAGING_READ_ONLY.value,
                        workflow.stem,
                        job.key,
                        step.line,
                        f"{workflow.path}:{step.line}: packaging workflow must not publish ({banned!r})",
                    )


def ci_gate_jobs_start_from_prepare(workflow: Workflow, job: Job) -> Iterator[Finding]:
    if workflow.stem != "ci":
        return
    if job.key in {"audit", "secret-scan", "dependency-review"}:
        return
    if job.key not in REPORTING_JOBS:
        return
    uses_prepare = any(
        step.uses and str(step.uses).startswith("$/.github/actions/prepare") for step in job.steps
    )
    if not uses_prepare:
        yield Finding(
            RuleId.CI_GATE_JOBS_START_FROM_PREPARE.value,
            workflow.stem,
            job.key,
            job.line,
            f"{workflow.path}:{job.line}: gate job must start from $/.github/actions/prepare",
        )


def ci_security_permissions(workflow: Workflow) -> Iterator[Finding]:
    if workflow.stem != "ci":
        return
    if dict(workflow.permissions) != {"contents": "read", "pull-requests": "read"}:
        yield Finding(
            RuleId.CI_SECURITY_PERMISSIONS.value,
            workflow.stem,
            "",
            1,
            f"{workflow.path}: CI must declare exactly contents:read and pull-requests:read",
        )


def ci_release_gated_on_ci(workflow: Workflow) -> Iterator[Finding]:
    """Every release job but the read-only roster must depend on ci transitively."""
    if workflow.stem != "release":
        return
    jobs = workflow.jobs
    gated = {"ci"} if "ci" in jobs else set()
    changed = True
    while changed:
        changed = False
        for name, job in jobs.items():
            if name not in gated and set(job.needs) & gated:
                gated.add(name)
                changed = True
    for name, job in jobs.items():
        if name not in gated and name != "released":
            yield Finding(
                RuleId.CI_RELEASE_GATED_ON_CI.value,
                workflow.stem,
                name,
                job.line,
                f"{workflow.path}:{job.line}: {name} is not downstream of the ci gate",
            )
