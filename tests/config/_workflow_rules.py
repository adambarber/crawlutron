"""CI/release/packaging rules over the shared typed workflow model."""

from __future__ import annotations

from collections.abc import Iterator

from tests.config._bundle import Bundle, _finding, _job, _workflow
from tests.config._policy import Finding, RuleId


def ci_supply_chain_jobs(bundle: Bundle) -> Iterator[Finding]:
    """The aggregate depends on audit and secret-scan gates that really run."""
    ci = _workflow(bundle, "ci")
    for key in ("audit", "secret-scan"):
        job = ci.jobs.get(key)
        if job is None:
            yield _finding(RuleId.CI_SUPPLY_CHAIN_JOBS, ci.path.name, 1, f"ci must declare the {key} job")
            continue
        if key not in bundle.reporting_jobs:
            yield _finding(
                RuleId.CI_SUPPLY_CHAIN_JOBS, ci.path.name, job.line, f"{key} must be a reporting job"
            )
        runs = " ".join(str(step.run or "") for step in job.steps)
        if key == "audit":
            for needle in ("uv export --frozen", "pip-audit"):
                if needle not in runs:
                    yield _finding(
                        RuleId.CI_SUPPLY_CHAIN_JOBS, ci.path.name, job.line, f"audit must run {needle}"
                    )
        if "gitleaks.io" in runs:
            yield _finding(
                RuleId.CI_SUPPLY_CHAIN_JOBS,
                ci.path.name,
                job.line,
                "a mutable gitleaks.io ref is not allowed",
            )


def ci_actionlint_in_reporting(bundle: Bundle) -> Iterator[Finding]:
    """actionlint reports a result, so the required aggregate must depend on it."""
    if "actionlint" not in bundle.reporting_jobs:
        yield _finding(RuleId.CI_ACTIONLINT_IN_REPORTING, "ci.yml", 1, "actionlint must be a reporting job")


def packaging_triggers(bundle: Bundle) -> Iterator[Finding]:
    """Packaging is reusable only; a push or workflow_run trigger bypasses CI."""
    packaging = _workflow(bundle, "packaging")
    if set(packaging.on_) != {"workflow_call"}:
        yield _finding(
            RuleId.PACKAGING_TRIGGERS,
            packaging.path.name,
            1,
            f"packaging triggers {sorted(packaging.on_)} must be only workflow_call",
        )


def packaging_verify_steps(bundle: Bundle) -> Iterator[Finding]:
    """Every distribution is inspected and installed before packaging is green."""
    packaging = _workflow(bundle, "packaging")
    for needle in ("twine check --strict", "scripts.check_packaging"):
        if not any(needle in str(step.run or "") for job in packaging.jobs.values() for step in job.steps):
            yield _finding(
                RuleId.PACKAGING_VERIFY_STEPS, packaging.path.name, 1, f"packaging must run {needle}"
            )


JUNIT_SHAPE = {
    "battery": ("junit-results", "junit-*.xml"),
    "real-pbkdf2": ("junit-real-pbkdf2", "junit-real-pbkdf2-*.xml"),
    "test-browser": ("junit-test-browser", "junit-test-browser-*.xml"),
}
DOWNLOAD_GUARDS = {
    "coverage-data": "${{ always() }}",
    "junit-results": "${{ always() }}",
    "coverage-data-real-pbkdf2": "${{ always() && needs.changes.outputs.real-pbkdf2 == 'true' }}",
    "junit-real-pbkdf2": "${{ always() && needs.changes.outputs.real-pbkdf2 == 'true' }}",
    "coverage-data-test-browser": "${{ always() && needs.test-browser.result != 'skipped' }}",
    "junit-test-browser": "${{ always() && needs.test-browser.result != 'skipped' }}",
}
JUNIT_FILES = {
    "behavioral": "junit/split/behavioral/junit-*.xml",
    "invariants": "junit/split/invariants/junit-*.xml",
    "real-pbkdf2": "junit/real-pbkdf2/junit-real-pbkdf2-*.xml",
    "test-browser": "junit/test-browser/junit-test-browser-*.xml",
}


def ci_junit_upload_shape(bundle: Bundle) -> Iterator[Finding]:
    """Each surface uploads one exactly named junit artifact, unconditionally."""
    ci = _workflow(bundle, "ci")
    for key, (name, path) in JUNIT_SHAPE.items():
        job = _job(bundle, "ci", key)
        if job is None:
            yield _finding(RuleId.CI_JUNIT_UPLOAD_SHAPE, "ci.yml", 1, f"ci has no {key!r} job")
            continue
        uploads = [
            step
            for step in job.steps
            if step.uses and "upload-artifact" in step.uses and "junit" in str(step.name or "").lower()
        ]
        if len(uploads) != 1:
            yield _finding(
                RuleId.CI_JUNIT_UPLOAD_SHAPE,
                "ci.yml",
                job.line,
                f"{key} must upload exactly one junit artifact",
            )
            continue
        step = uploads[0]
        for field, expected in (
            ("name", name),
            ("path", path),
            ("if-no-files-found", "error"),
        ):
            if step.with_.get(field) != expected:
                yield _finding(
                    RuleId.CI_JUNIT_UPLOAD_SHAPE,
                    "ci.yml",
                    step.line,
                    f"{key} junit upload {field} must be {expected!r}",
                )
        if str(step.if_ or "") != "${{ always() }}":
            yield _finding(
                RuleId.CI_JUNIT_UPLOAD_SHAPE,
                ci.path.name,
                step.line,
                f"{key} junit upload must run if: always()",
            )


def ci_junit_split_order(bundle: Bundle) -> Iterator[Finding]:
    """Battery junit is split once, before the two split-flagged Codecov uploads."""
    job = _job(bundle, "ci", "coverage")
    if job is None:
        yield _finding(RuleId.CI_JUNIT_SPLIT_ORDER, "ci.yml", 1, "ci has no coverage job")
        return
    steps = job.steps
    splits = [
        i
        for i, step in enumerate(steps)
        if "scripts.split_junit junit/battery junit/split" in str(step.run or "")
    ]
    if len(splits) != 1:
        yield _finding(
            RuleId.CI_JUNIT_SPLIT_ORDER,
            _workflow(bundle, "ci").path.name,
            job.line,
            f"junit must be split exactly once, found {len(splits)}",
        )
        return
    uploads = [
        (i, step)
        for i, step in enumerate(steps)
        if step.uses and "codecov" in step.uses and "junit/split/" in str(step.with_.get("files", ""))
    ]
    if len(uploads) != 2:
        yield _finding(
            RuleId.CI_JUNIT_SPLIT_ORDER,
            _workflow(bundle, "ci").path.name,
            job.line,
            f"expected two split junit uploads, found {len(uploads)}",
        )
    if any(index <= splits[0] for index, _ in uploads):
        yield _finding(
            RuleId.CI_JUNIT_SPLIT_ORDER,
            _workflow(bundle, "ci").path.name,
            job.line,
            "a split junit upload runs before the split",
        )
    for _, step in uploads:
        flag = str(step.with_.get("flags", ""))
        expected_if = (
            "${{ !cancelled() && needs.changes.outputs.real-pbkdf2 == 'true' }}"
            if flag == "real-pbkdf2"
            else "${{ !cancelled() }}"
        )
        if flag in JUNIT_FILES and (
            step.with_.get("files") != JUNIT_FILES[flag] or str(step.if_ or "") != expected_if
        ):
            yield _finding(
                RuleId.CI_JUNIT_SPLIT_ORDER,
                _workflow(bundle, "ci").path.name,
                step.line,
                f"codecov upload flag {flag!r} lost its exact files or if guard",
            )


def ci_coverage_download_guards(bundle: Bundle) -> Iterator[Finding]:
    """Tolerant coverage downloads carry the exact per-surface skip guard."""
    job = _job(bundle, "ci", "coverage")
    if job is None:
        yield _finding(RuleId.CI_COVERAGE_DOWNLOAD_GUARDS, "ci.yml", 1, "ci has no coverage job")
        return
    for name, guard in DOWNLOAD_GUARDS.items():
        steps = [
            step
            for step in job.steps
            if step.uses and "download-artifact" in step.uses and step.with_.get("name") == name
        ]
        if len(steps) != 1 or str(steps[0].if_ or "") != guard:
            line = steps[0].line if steps else job.line
            yield _finding(
                RuleId.CI_COVERAGE_DOWNLOAD_GUARDS,
                _workflow(bundle, "ci").path.name,
                line,
                f"download {name!r} must use guard {guard!r}",
            )


def ci_no_direct_codecov(bundle: Bundle) -> Iterator[Finding]:
    """Only the coverage job uploads to Codecov; the two test jobs stay clean."""
    for key in ("real-pbkdf2", "test-browser"):
        job = _job(bundle, "ci", key)
        if job is None:
            continue
        for step in job.steps:
            if step.uses and "codecov" in step.uses:
                yield _finding(
                    RuleId.CI_NO_DIRECT_CODECOV,
                    _workflow(bundle, "ci").path.name,
                    step.line,
                    f"{key} must not upload directly to Codecov",
                )


def release_triggers(bundle: Bundle) -> Iterator[Finding]:
    """Release fires on named tags and dispatch, never a branch or workflow_run."""
    release = _workflow(bundle, "release")
    path = release.path.name
    if set(release.on_) != {"push", "workflow_dispatch"}:
        yield _finding(
            RuleId.RELEASE_TRIGGERS,
            path,
            1,
            f"release triggers {sorted(release.on_)} must be push and dispatch",
        )
    push = release.on_.get("push")
    patterns = {str(tag) for tag in push.get("tags", ())} if isinstance(push, dict) else set()
    if "v*" not in patterns:
        yield _finding(RuleId.RELEASE_TRIGGERS, path, 1, "root tags v* must trigger a release")
    if "*-v*" in patterns:
        yield _finding(RuleId.RELEASE_TRIGGERS, path, 1, "a wildcard around -v matches unrelated tags")
    for pattern in sorted(patterns):
        if pattern != "v*" and not pattern.startswith("crawlutron"):
            yield _finding(
                RuleId.RELEASE_TRIGGERS, path, 1, f"component tag {pattern!r} must be named explicitly"
            )
