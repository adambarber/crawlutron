"""CI runs every pre-commit hook through one surface, without reruns."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from scripts import jscpd_gate as gate
from tests._ci_contract import (
    CONFIG_PATH,
    ENFORCEMENT_TRIGGER_PATTERN,
    PACKAGING_WORKFLOW_PATH,
    PREPARE_ACTION_PATH,
    RELEASE_WORKFLOW_PATH,
    REPORTING_JOBS,
    SERVICE_ROOT,
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
    job_steps,
    precommit_hook_ids,
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
    """#623: supply-chain gates run pip-audit and gitleaks as required jobs."""
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
    """Only a CI-gated workflow may publish; ci.yml stays read-all."""
    document = _workflow_document(RELEASE_WORKFLOW_PATH)
    triggers = _triggers(document)
    # Publishing is downstream of CI, never of the tag push itself: the tag makes
    # it a release, a green CI run is what makes it publishable.
    assert set(triggers) == {"push", "workflow_dispatch"}, (
        f"the release workflow must be triggered by tag push or dispatch, found triggers {sorted(triggers)}"
    )
    push = _mapping(triggers.get("push"), "a push gate")
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
    jobs = _mapping(document.get("jobs"), "jobs")
    ci = _mapping(jobs.get("ci"), "the ci job")
    assert ci.get("uses") == "$/.github/workflows/ci.yml", (
        "the ci job must call ci.yml as a reusable workflow"
    )
    _assert_every_publish_job_gated_on_ci(jobs)


def _assert_every_publish_job_gated_on_ci(jobs: dict[str, object]) -> None:
    """Every publishing/admission job transitively depends on `ci`, so a red
    commit never reaches them: `needs` replaces `workflow_run`."""
    # `released` is a read-only roster resolver that runs ahead of CI to elect
    # one publishing tag; every other job must still be downstream of CI.
    released_job = _mapping(jobs.get("released"), "the released job")
    assert released_job.get("needs") in (None, [])
    assert _mapping(released_job.get("permissions"), "released permissions") == {"contents": "read"}
    gated = {"ci"}
    changed = True
    while changed:
        changed = False
        for name, job in jobs.items():
            if name in gated or name == "released":
                continue
            needed = _mapping(job, name).get("needs") or []
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

    image = _mapping(jobs.get("image"), "an image job")
    assert _mapping(image.get("permissions"), "image permissions").get("packages") == "write", (
        "the image job needs packages:write, scoped to itself"
    )
    publish = _mapping(jobs.get("publish"), "a publish job")
    assert _mapping(publish.get("permissions"), "publish permissions").get("id-token") == "write", (
        "the publish job needs id-token:write for trusted publishing"
    )
    # The tag is what makes it a release: publication is derived from the tags at
    # the commit CI passed, never from the merge itself.
    released_runs = " ".join(str(step.get("run", "")) for step in _steps(jobs.get("released")))
    assert "release_roster.py --released" in released_runs, (
        "the released set must come from the tags pointing at the green commit"
    )
    build = _mapping(jobs.get("publish-build"), "a publish-build job")
    needs = build.get("needs")
    assert isinstance(needs, list), "the build must declare its upstream jobs"
    assert {str(item) for item in needs} == {"released", "ci"}, (
        "the build must be gated on both the resolved roster and green CI"
    )
    assert "id-token" not in _mapping(build.get("permissions"), "build permissions"), (
        "a build backend must run without OIDC in scope"
    )
    build_steps = [str(step.get("name", "")) for step in _steps(jobs.get("publish-build"))]
    assert "Assert the artifacts carry the release version" in build_steps, (
        "a build whose version disagrees with its tag must never reach the index"
    )
    assert "needs.released.outputs.packages" in str(publish.get("if")), (
        "the publish job must only upload the resolved packages"
    )


def _workflow_document(path: Path) -> dict[object, object]:
    """One workflow file, parsed. The single reader, so every check sees one shape."""
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict), f"{path.name} is not a workflow"
    return document


def _mapping(value: object, what: str) -> dict[str, object]:
    """A workflow block narrowed to a string-keyed mapping, or a named refusal."""
    assert isinstance(value, dict), f"the workflow must declare {what}"
    return {str(key): item for key, item in value.items()}


def _steps(job: object) -> list[dict[str, object]]:
    """One job's step records, each narrowed to a string-keyed mapping."""
    steps = _mapping(job, "a job").get("steps")
    assert isinstance(steps, list)
    return [_mapping(step, "a step") for step in steps]


def _triggers(document: dict[object, object]) -> dict[str, object]:
    """The ``on`` block, whichever spelling the YAML parser used for the key."""
    raw = document.get("on")
    return _mapping(raw if isinstance(raw, dict) else document.get(True), "an 'on' trigger")


def test_packaging_runs_only_after_a_green_ci_run() -> None:
    """Packaging runs only after green CI; running in parallel is ambiguous."""
    assert PACKAGING_WORKFLOW_PATH.is_file(), "a packaging workflow must exist"
    document = _workflow_document(PACKAGING_WORKFLOW_PATH)
    assert "workflow_call" in _triggers(document), "packaging is a reusable workflow"
    assert "push" not in _triggers(document), "packaging must not run on the push itself"
    assert "workflow_run" not in _triggers(document), "packaging must not use workflow_run"


def test_packaging_workflow_publishes_nothing() -> None:
    """Every commit proves packaging; only a tag may publish."""
    assert PACKAGING_WORKFLOW_PATH.is_file(), "a packaging workflow must exist"
    workflow = PACKAGING_WORKFLOW_PATH.read_text(encoding="utf-8")
    document = yaml.safe_load(workflow)
    assert isinstance(document, dict)
    assert document.get("permissions") == {"contents": "read"}, (
        "the packaging workflow needs only its read scope"
    )
    for banned in ("pypa/gh-action-pypi-publish", "twine upload", "docker push", "push: true"):
        assert banned not in workflow, f"the packaging workflow must not publish ({banned!r})"
    assert "twine check --strict" in workflow, "the packaging workflow must inspect every artifact"
    assert "scripts/check_packaging.py" in workflow, "every wheel must install and import"


def test_every_precommit_hook_has_a_ci_job_that_runs_the_hook() -> None:
    """Every pre-commit hook has a CI job running the hook itself."""
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


def _cache_step_paths(action: str) -> str:
    """The ``path:`` block of the action's cache step, as one string.

    Scoped to that block so a comment elsewhere explaining what must NOT be cached
    cannot satisfy (or defeat) the check.
    """
    lines = action.splitlines()
    start = next((i for i, line in enumerate(lines) if "uses: actions/cache@" in line), None)
    assert start is not None, "no cache step"
    path_indent = None
    body: list[str] = []
    for line in lines[start + 1 :]:
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if path_indent is None:
            if line.strip().startswith("path:"):
                path_indent = indent
                body.append(line.strip())
            continue
        if indent <= path_indent:
            break
        body.append(line.strip())
    return "\n".join(body)


def test_the_prepare_action_installs_the_frozen_lock() -> None:
    """One gate environment, defined once."""
    action = PREPARE_ACTION_PATH.read_text(encoding="utf-8")
    assert 'python-version: "3.14"' in action, "the prepare action must pin the 3.14 interpreter"
    assert "uv sync --frozen --extra dev" in action, (
        "the prepare action must install from the frozen lock: a bare install "
        "re-resolves the graph CI never froze"
    )
    assert "pip install" not in action, "the prepare action installs with uv, not pip"
    assert "actions/cache@" in action, "the prepare action must cache gate state"
    assert "actions/checkout" not in action, (
        "checkout belongs in the job: GitHub resolves a local action before any step "
        "runs, so an action that checks out runs before the repo is on disk"
    )
    # Per-run pidfiles must never ride a shared cache: every gate job uses this
    # cache key, so a pid from one runner read on another is a stranger's process.
    cached = _cache_step_paths(action)
    assert cached, "the prepare action must declare a cache step with path inputs"
    assert ".gate_fanout" not in cached, (
        "the gate-fanout status dir holds per-run pidfiles and must not be cached"
    )
    for cache_dir in (".mypy_cache", ".ruff_cache"):
        assert cache_dir in cached, f"the prepare action must cache {cache_dir}"


def test_every_gate_job_starts_from_the_prepare_action() -> None:
    """The one environment is the one every gate uses: a job that repeats the
    install steps by hand is a second copy that can drift.
    """
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    for name in REPORTING_JOBS:
        if name in {"audit", "secret-scan"}:
            continue
        steps = job_steps(workflow, name)
        assert any(str(step.get("uses", "")).startswith("$/.github/actions/prepare") for step in steps), (
            f"job {name} must start from $/.github/actions/prepare: the self-repository "
            "form cannot resolve to another commit (zizmor self-repository)"
        )
        assert any(str(step.get("uses", "")).split("@")[0] == "actions/checkout" for step in steps), (
            f"job {name} must check out before using the prepare action"
        )
    for name, run in _job_run_steps(workflow):
        assert "pip install" not in run, (
            f"job {name} installs with pip: the lock freezes the graph and pip re-resolves it"
        )


def test_no_ci_job_reruns_the_default_suite() -> None:
    """The battery hook runs the whole default suite; a bare pytest is a second copy."""
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    _assert_no_default_suite_rerun(workflow)


def test_a_ci_job_that_reruns_the_default_suite_is_caught() -> None:
    """Bites: re-adding a bare-pytest job fails the check."""
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    needle = "  test-browser:"
    inserted = (
        "  test-fast:\n"
        "    name: Test suite (fast)\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - name: Run pytest (fast)\n"
        "        run: .venv/bin/python -m pytest\n\n"
        "  test-browser:"
    )
    broken = workflow.replace(needle, inserted)
    assert broken != workflow, "the injection did not apply; the probe proves nothing"
    with pytest.raises(AssertionError):
        _assert_no_default_suite_rerun(broken)


def _assert_no_default_suite_rerun(workflow: str) -> None:
    """The check above, callable on a mutated workflow so the probe can bite.

    Only pytest's own args count: the interpreter's ``-m`` is not a marker filter.
    """
    for name, run in _job_run_steps(workflow):
        if "pytest" not in run:
            continue
        pytest_args = run.split("pytest", 1)[1]
        assert re.search(r"(?:^|\s)-m(?:\s|=)", pytest_args), (
            f"job {name} runs the default suite ({run.strip()!r}): enforcement's "
            "battery hook already runs it; give a second suite its own marker filter"
        )


def test_browser_job_selects_only_browser_marked_tests() -> None:
    """The browser job runs only browser-marked tests."""
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    steps = job_steps(workflow, "test-browser")
    browser_step = str(
        next(
            step.get("run")
            for step in steps
            if isinstance(step, dict) and "Run pytest (browser)" == str(step.get("name"))
        )
    )
    assert browser_step.strip() == 'xvfb-run -a .venv/bin/python -m pytest -m "browser"', browser_step


def test_browser_job_installs_pinned_fpgen_model_before_tests() -> None:
    """A warm camoufox cache skips its fetch, the only fpgen model installer."""
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    steps = job_steps(workflow, "test-browser")
    pytest_index = next(i for i, step in enumerate(steps) if str(step.get("name")) == "Run pytest (browser)")
    installers = [
        (i, step) for i, step in enumerate(steps) if "ensure_fpgen_model" in str(step.get("run", ""))
    ]
    assert len(installers) == 1, installers
    index, installer = installers[0]
    assert index < pytest_index, "the model must be installed before pytest starts"
    assert "if" not in installer, "a warm browser cache must not skip the model install"
    assert "camoufox.fpgen_model" in str(installer["run"])
    assert "python -m fpgen fetch" not in str(installer["run"])


def test_conftest_leaves_bytecode_writing_enabled() -> None:
    """Conftest must not set PYTHONDONTWRITEBYTECODE so modules compile to .pyc."""
    import os

    import tests.conftest as conftest_module

    _ = conftest_module  # conftest's import side effect applies its env at startup
    assert os.environ.get("PYTHONDONTWRITEBYTECODE") != "1", (
        "conftest disables bytecode writes; remove the PYTHONDONTWRITEBYTECODE=1 line"
    )


def test_ci_precompiles_bytecode_before_the_battery() -> None:
    """The battery job precompiles .pyc once after install for worker reuse."""
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    steps = job_steps(workflow, "battery")
    assert steps
    runs = " ".join(str(step.get("run", "")) for step in steps if isinstance(step, dict))
    assert "compileall" in runs, "the enforcement job must precompile bytecode with compileall"
    # #949: the compile scope derives from the uv member src roots (glob, never a
    # hand-listed pre-src-layout package dir); this pins that derived glob.
    assert "packages/*/src" in runs, "compileall must derive its scope from the member src roots"
    assert "tests" in runs and "scripts" in runs, "compileall must cover the test and script trees"


def test_ci_release_workflow_pins_version_consistency() -> None:
    """The smoke asserts the BAKED version equals the tag it was pushed as."""
    text = RELEASE_WORKFLOW_PATH.read_text(encoding="utf-8")
    runs = [str(run) for _, run in _job_run_steps(text)]
    joined = "\n".join(runs)
    document = yaml.safe_load(text)
    registries = [
        str(step["with"]["registry"])
        for job in document.get("jobs", {}).values()
        if isinstance(job, dict)
        for step in job.get("steps", [])
        if isinstance(step, dict) and isinstance(step.get("with"), dict) and "registry" in step["with"]
    ]
    assert registries == ["ghcr.io"], registries
    assert 'docker push "$IMAGE"' in joined, "the release workflow must push the pinned image"
    assert "tr '[:upper:]' '[:lower:]'" in joined, "GHCR tag must be lowercased"
    # The derived version reaches the smoke through env (zizmor template-injection),
    # so the binding is asserted on the workflow text and its use on the script.
    assert "EXPECTED_VERSION: ${{ steps.version.outputs.native }}" in text, (
        "the smoke must compare the baked version against the git-describe version"
    )
    assert 'test "$BAKED" = "$EXPECTED_VERSION"' in joined
    # The image's CMD binds loopback by default; Docker -p cannot reach a
    # loopback-bound process, so the smoke must bind 0.0.0.0 (compose precedent).
    assert "-e CRAWLUTRON_HOST=0.0.0.0" in joined, (
        "the smoke must set CRAWLUTRON_HOST=0.0.0.0 for -p to reach the service"
    )


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
