"""CI runs every pre-commit hook through one surface, without reruns."""

from __future__ import annotations

import re

import pytest

from scripts import jscpd_gate as gate
from tests._ci_contract import (
    CONFIG_PATH,
    ENFORCEMENT_TRIGGER_PATTERN,
    RELEASE_WORKFLOW_PATH,
    REPORTING_JOBS,
    WORKFLOW_PATH,
    _assert_failure_propagation,
    _assert_no_config_exclude,
    _assert_no_continue_on_error,
    _entry,
    _field,
    _hook_blocks,
    _job_run_steps,
    _jobs,
    _require_exactly_one_jscpd_gate,
    _required_needs,
    hook_run,
    mapping,
    precommit_hook_ids,
    steps,
    triggers,
    workflow_document,
)


def test_ci_runs_every_precommit_hook_through_one_surface() -> None:
    config = CONFIG_PATH.read_text(encoding="utf-8")
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    hooks = _hook_blocks(config)
    assert hooks
    _assert_no_config_exclude(config)

    jscpd_id, jscpd = _require_exactly_one_jscpd_gate(config)
    assert jscpd_id == "jscpd"
    assert "jscpd-app" not in config
    assert "jscpd-tests" not in config
    assert not [hook_id for hook_id, _ in hooks if hook_id in {"jscpd-app", "jscpd-tests"}]

    # #557: coverage runs at pre-push (per-commit measurement trains
    # --no-verify) and stays out of CI's enforcement surface.
    assert all(block.get("stages") in (["pre-commit"], ["commit-msg"], ["pre-push"]) for _, block in hooks)
    coverage_hooks = [(hook_id, block) for hook_id, block in hooks if hook_id == "coverage"]
    assert len(coverage_hooks) == 1, "exactly one coverage surface hook"
    assert coverage_hooks[0][1].get("stages") == ["pre-push"], (
        "the coverage hook must run at pre-push, not per-commit"
    )
    assert "--cov-fail-under" not in config, (
        "coverage is measured-descriptive; a threshold is an owner ruling, not a default"
    )

    entry = _entry(jscpd)
    assert ".venv/bin/python -m scripts.jscpd_gate all" in entry
    # The floor is on the command line rather than inside the gate so the command stays the one place a
    # reviewer looks to see what duplication costs
    floor = re.search(r"--min-lines (\d+) --min-tokens (\d+) --threshold (\S+)", entry)
    assert floor is not None, f"the entry states no single floor triple: {entry!r}"
    assert floor.group(1) == str(gate.configured_floor()[0])
    assert floor.group(2) == str(gate.configured_floor()[1])
    assert float(floor.group(3)) == gate.configured_floor()[2]
    assert "--app-min-lines" not in entry, (
        "the per-tree floor split is gone; one number must serve both trees"
    )
    assert _field(jscpd, "files") == ENFORCEMENT_TRIGGER_PATTERN

    assert workflow.count(".venv/bin/pre-commit run") == len(
        precommit_hook_ids(CONFIG_PATH.read_text(encoding="utf-8"))
    )
    assert "scripts/jscpd_gate.py" not in workflow
    required = _required_needs(workflow)
    assert required == REPORTING_JOBS
    # The enforcement job builds its venv from the frozen lock's dev extra;
    # re-installing a specific tool on top reintroduces a drifting declaration.
    _assert_failure_propagation(workflow)


def test_ci_runs_a_dependency_audit_and_secret_scan() -> None:
    """#623: supply-chain gates. pip-audit against the frozen lock's export
    catches known CVEs; gitleaks catches committed secrets. Both run as jobs
    the required aggregate depends on, so skipping one is not green.
    """
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    jobs = _jobs(workflow)
    for job in ("audit", "secret-scan"):
        assert job in jobs, f"CI must declare the {job} job"
        _assert_no_continue_on_error(jobs[job], job)

    required = _required_needs(workflow)
    assert {"audit", "secret-scan"} <= required, (
        "the required aggregate must depend on the audit and secret-scan jobs"
    )

    def job_runs(job: dict[str, object]) -> str:
        steps = job.get("steps")
        assert isinstance(steps, list)
        return " ".join(str(step.get("run")) for step in steps if isinstance(step, dict) and "run" in step)

    audit_runs = job_runs(jobs["audit"])
    assert "uv export --frozen" in audit_runs, "audit must export the frozen lock, not live deps"
    assert "pip-audit" in audit_runs, "audit must run pip-audit"

    assert "gitleaks.io" not in workflow, "workflow must not use a mutable gitleaks.io ref"


def test_release_workflow_publishes_nothing_without_a_tag() -> None:
    """#949/#1014: only a CI-gated workflow may publish, and ci.yml cannot.

    ci.yml stays read-all, so the write scope lives here and only here; the jobs
    that do not publish must not inherit it.
    """
    document = workflow_document(RELEASE_WORKFLOW_PATH)
    triggers_dict = triggers(document)
    # Publishing is downstream of CI, never of the tag push itself: the tag makes
    # it a release, a green CI run is what makes it publishable.
    assert set(triggers_dict) == {"push", "workflow_dispatch"}, (
        f"triggers are {sorted(triggers_dict)}; expected push and workflow_dispatch"
    )
    push = mapping(triggers_dict.get("push"), "a push gate")
    tags = push.get("tags")
    assert isinstance(tags, list), "the release push gate must declare tag patterns"
    patterns = {str(tag) for tag in tags}
    assert "v*" in patterns, "root tags must trigger a release"
    assert "*-v*" not in patterns, "a wildcard around -v matches unrelated tags"
    assert all(pattern == "v*" or pattern.startswith("crawlutron") for pattern in patterns), (
        "component tag patterns must be named explicitly"
    )

    # The `ci` job calls ci.yml as a reusable workflow; `needs` is the gate --
    # no `workflow_run` trigger.
    jobs = mapping(document.get("jobs"), "jobs")
    ci = mapping(jobs.get("ci"), "the ci job")
    assert ci.get("uses") == "$/.github/workflows/ci.yml", (
        "the ci job must call ci.yml as a reusable workflow"
    )
    _assert_every_publish_job_gated_on_ci(jobs)


def _assert_every_publish_job_gated_on_ci(jobs: dict[str, object]) -> None:
    """Every publishing/admission job transitively depends on `ci`, so a red
    commit never reaches them: `needs` replaces `workflow_run`."""
    # `released` is a read-only roster resolver that runs ahead of CI to elect
    # one publishing tag; every other job must still be downstream of CI.
    released_job = mapping(jobs.get("released"), "the released job")
    assert released_job.get("needs") in (None, [])
    assert mapping(released_job.get("permissions"), "released permissions") == {"contents": "read"}
    gated = {"ci"}
    changed = True
    while changed:
        changed = False
        for name, job in jobs.items():
            if name in gated or name == "released":
                continue
            needed = mapping(job, name).get("needs") or []
            if isinstance(needed, str):
                upstream = {needed}
            else:
                assert isinstance(needed, list)
                upstream = {str(item) for item in needed}
            if upstream & gated:
                gated.add(name)
                changed = True
    outside = set(jobs) - {"released"} - gated
    assert not outside, f"jobs outside the CI gate: {sorted(outside)}"

    image = mapping(jobs.get("image"), "an image job")
    assert mapping(image.get("permissions"), "image permissions").get("packages") == "write", (
        "the image job needs packages:write, scoped to itself"
    )
    publish = mapping(jobs.get("publish"), "a publish job")
    assert mapping(publish.get("permissions"), "publish permissions").get("id-token") == "write", (
        "the publish job needs id-token:write for trusted publishing"
    )
    # The tag is what makes it a release: publication is derived from the tags at
    # the commit CI passed, never from the merge itself.
    released_runs = " ".join(str(step.get("run", "")) for step in steps(jobs.get("released")))
    assert "release_roster.py --released" in released_runs, (
        "the released set must come from the tags pointing at the green commit"
    )
    build = mapping(jobs.get("publish-build"), "a publish-build job")
    needs = build.get("needs")
    assert isinstance(needs, list), "the build must declare its upstream jobs"
    assert {str(item) for item in needs} == {"released", "ci"}, (
        "the build must be gated on both the resolved roster and green CI"
    )
    assert "id-token" not in mapping(build.get("permissions"), "build permissions"), (
        "a build backend must run without OIDC in scope"
    )
    build_steps = [str(step.get("name", "")) for step in steps(jobs.get("publish-build"))]
    assert "Assert the artifacts carry the release version" in build_steps, (
        "a build whose version disagrees with its tag must never reach the index"
    )
    assert "needs.released.outputs.packages" in str(publish.get("if")), (
        "the publish job must only upload the resolved packages"
    )


def test_every_precommit_hook_has_a_ci_job_that_runs_the_hook() -> None:
    """One definition per gate: every pre-commit hook has a job, and that job runs
    the hook itself. CI therefore cannot drift from what a local commit runs, and
    a new hook cannot arrive without a job that enforces it.
    """
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    config = CONFIG_PATH.read_text(encoding="utf-8")
    _assert_every_hook_has_a_job(workflow, precommit_hook_ids(config))


def test_a_new_precommit_hook_without_a_ci_job_is_caught() -> None:
    """Bites: adding a hook the workflow does not run fails the check."""
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    config = CONFIG_PATH.read_text(encoding="utf-8")
    with pytest.raises(AssertionError):
        _assert_every_hook_has_a_job(workflow, (*precommit_hook_ids(config), "drift-gate"))


def _assert_every_hook_has_a_job(workflow: str, hook_ids) -> None:
    """The check above, callable with an injected hook id so the probe can bite."""
    runs = [run.strip() for _, run in _job_run_steps(workflow)]
    for hook_id in hook_ids:
        assert hook_run(hook_id) in runs, (
            f"hook {hook_id!r} has no CI job running `{hook_run(hook_id)}`: "
            "CI must enforce every pre-commit hook, not a subset"
        )
