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
    PREPARE_ACTION_PATH,
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

# The version-triggered GHCR publish workflow (#949), contracted alongside ci.yml.
RELEASE_WORKFLOW_PATH = SERVICE_ROOT / ".github" / "workflows" / "release.yml"
PACKAGING_WORKFLOW_PATH = SERVICE_ROOT / ".github" / "workflows" / "packaging.yml"


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


def test_every_action_is_sha_pinned_and_permissions_are_declared() -> None:
    """#624: mutable tags (`@v4`) are repointable; every `uses:` must pin a
    full-length commit SHA, and the workflow must declare its permissions so
    the default write-all token scope cannot leak into a compromised step.
    """
    for workflow_path in (WORKFLOW_PATH, RELEASE_WORKFLOW_PATH, PACKAGING_WORKFLOW_PATH):
        document = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
        assert isinstance(document, dict)

        def walk(value: object, file_name: str = workflow_path.name) -> None:
            if isinstance(value, dict):
                uses = value.get("uses")
                if isinstance(uses, str) and "@" in uses:
                    ref = uses.rsplit("@", 1)[1]
                    assert re.fullmatch(r"[0-9a-f]{40}", ref), (
                        f"action ref {uses!r} in {file_name} is not a full-length commit SHA"
                    )
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(document.get("jobs"))
    ci = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert isinstance(ci, dict)
    permissions = ci.get("permissions")
    assert permissions == "read-all", (
        f"CI must declare top-level permissions: read-all, found {permissions!r}"
    )


def test_release_workflow_publishes_nothing_without_a_tag() -> None:
    """#949/#1014: only a CI-gated workflow may publish, and ci.yml cannot.

    ci.yml stays read-all, so the write scope lives here and only here; the jobs
    that do not publish must not inherit it.
    """
    document = _workflow_document(RELEASE_WORKFLOW_PATH)
    triggers = _triggers(document)
    # Publishing is downstream of CI, never of the tag push itself: the tag makes
    # it a release, a green CI run is what makes it publishable.
    assert set(triggers) == {"workflow_run"}, (
        f"the release workflow must be gated on CI, found triggers {sorted(triggers)}"
    )
    gated = _mapping(triggers["workflow_run"], "a workflow_run gate")
    assert gated.get("workflows") == ["CI"], "the release must follow the CI workflow"
    assert "completed" in str(gated.get("types")), "the release must run on CI completion"

    _assert_ci_gated(document, require_tag=True)
    jobs = _mapping(document.get("jobs"), "jobs")
    image = _mapping(jobs.get("image"), "an image job")
    assert _mapping(image.get("permissions"), "image permissions").get("packages") == "write", (
        "the image job needs packages:write, scoped to itself"
    )
    publish = _mapping(jobs.get("distributions"), "a distributions job")
    assert _mapping(publish.get("permissions"), "publish permissions").get("id-token") == "write", (
        "the publish job needs id-token:write for trusted publishing"
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


def _triggers(document: dict[object, object]) -> dict[str, object]:
    """The ``on`` block, whichever spelling the YAML parser used for the key."""
    raw = document.get("on")
    return _mapping(raw if isinstance(raw, dict) else document.get(True), "an 'on' trigger")


def _assert_ci_gated(document: dict[object, object], *, require_tag: bool) -> None:
    """Every job runs only after a green CI run, on the branches this workflow serves.

    ``require_tag`` adds the release's own condition: the tag makes it a release,
    the green run makes it publishable, and neither alone is sufficient.
    """
    gate = _mapping(_triggers(document).get("workflow_run"), "a workflow_run gate")
    assert gate.get("workflows") == ["CI"], "must follow CI"
    assert "completed" in str(gate.get("types")), "on completion"
    for name, job in _mapping(document.get("jobs"), "jobs").items():
        condition = str(_mapping(job, f"job {name}").get("if", ""))
        assert "workflow_run.conclusion == 'success'" in condition, (
            f"job {name} does not require a green CI run"
        )
        if require_tag:
            assert "startsWith(github.event.workflow_run.head_branch, 'v')" in condition, (
                f"job {name} does not require a v* tag"
            )


def test_packaging_runs_only_after_a_green_ci_run() -> None:
    """Packaging is downstream of the gates, so a red commit never reaches it.

    Running the two in parallel makes a failure ambiguous: a packaging break and a
    gate break look the same, and a red commit is judged on a build it never cleared.
    """
    assert PACKAGING_WORKFLOW_PATH.is_file(), "a packaging workflow must exist"
    document = _workflow_document(PACKAGING_WORKFLOW_PATH)
    assert "workflow_run" in _triggers(document), "packaging runs on a CI run's completion"
    assert "push" not in _triggers(document), "packaging must not run on the push itself"
    _assert_ci_gated(document, require_tag=False)


def test_packaging_workflow_publishes_nothing() -> None:
    """Every commit proves the packaging; only a tag may publish.

    A packaging workflow that could upload would make "released" and "merged"
    the same event, which is exactly what the tag gate exists to prevent.
    """
    assert PACKAGING_WORKFLOW_PATH.is_file(), "a packaging workflow must exist"
    workflow = PACKAGING_WORKFLOW_PATH.read_text(encoding="utf-8")
    document = yaml.safe_load(workflow)
    assert isinstance(document, dict)
    assert document.get("permissions") == "read-all", "the packaging workflow needs no write scope"
    for banned in ("pypa/gh-action-pypi-publish", "twine upload", "docker push", "push: true"):
        assert banned not in workflow, f"the packaging workflow must not publish ({banned!r})"
    assert "twine check --strict" in workflow, "the packaging workflow must inspect every artifact"
    assert "scripts/check_packaging.py" in workflow, "every wheel must install and import"


def test_release_please_is_gone() -> None:
    """#1014: two sources of truth for one version is the defect being removed.

    release-please edits a static version field, while this project's version is
    derived from the git tag, so it could never have worked.
    """
    for path in (
        ".github/release-please.yml",
        "release-please-config.json",
        ".release-please-manifest.json",
    ):
        assert not (SERVICE_ROOT / path).exists(), f"{path} is dead configuration: the tag is the version"
    workflow = SERVICE_ROOT.joinpath(".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "release-please" not in workflow


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


def test_the_prepare_action_installs_the_frozen_lock_and_regenerates_the_version() -> None:
    """One gate environment, defined once: every job starts from the same prepare
    action, so the frozen install, the 3.14 interpreter and the gitignored version
    artifact cannot drift between jobs.
    """
    action = PREPARE_ACTION_PATH.read_text(encoding="utf-8")
    assert 'python-version: "3.14"' in action, "the prepare action must pin the 3.14 interpreter"
    assert "uv sync --frozen --extra dev" in action, (
        "the prepare action must install from the frozen lock: a bare install "
        "re-resolves the graph CI never froze"
    )
    assert "scripts/regen_version.py" in action, (
        "the prepare action must regenerate the gitignored version artifact"
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
        assert any(str(step.get("uses", "")).startswith("./.github/actions/prepare") for step in steps), (
            f"job {name} must start from ./.github/actions/prepare"
        )
        assert any(str(step.get("uses", "")).split("@")[0] == "actions/checkout" for step in steps), (
            f"job {name} must check out before using the local prepare action"
        )
    for name, run in _job_run_steps(workflow):
        assert "pip install" not in run, (
            f"job {name} installs with pip: the lock freezes the graph and pip re-resolves it"
        )


def test_no_ci_job_reruns_the_default_suite() -> None:
    """`enforcement`'s battery hook already runs the whole default suite, so a
    bare `pytest` in another job is a second copy: the two can drift, and every
    push pays twice. A `-m` selection is a different surface, and may stand.
    """
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

    Only pytest's own args count: the interpreter's ``-m`` is not a marker
    filter, so a bare ``pytest`` and a ``-m browser`` run are told apart.
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
    """The browser job exists to run the browser suite: a filter that admits
    anything else re-runs the fast suite (~3,400 tests) on every push."""
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    steps = job_steps(workflow, "test-browser")
    browser_step = str(
        next(
            step.get("run")
            for step in steps
            if isinstance(step, dict) and "Run pytest (browser)" == str(step.get("name"))
        )
    )
    assert browser_step.strip() == '.venv/bin/python -m pytest -m "browser"', browser_step


def test_conftest_leaves_bytecode_writing_enabled() -> None:
    """S4: conftest must not set PYTHONDONTWRITEBYTECODE so modules compile to
    .pyc once and are reused across xdist workers and runs."""
    import os

    import tests.conftest as conftest_module

    _ = conftest_module  # conftest's import side effect applies its env at startup
    assert os.environ.get("PYTHONDONTWRITEBYTECODE") != "1", (
        "conftest disables bytecode writes; remove the PYTHONDONTWRITEBYTECODE=1 line"
    )


def test_ci_precompiles_bytecode_before_the_battery() -> None:
    """S4: the battery job precompiles .pyc once after install so every worker
    reuses them instead of recompiling ~90k lines per run."""
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
    """#949: the smoke asserts the BAKED version equals the tag it was pushed as,
    so the image cannot carry a version that disagrees with its :{version} tag."""
    runs = [str(run) for _, run in _job_run_steps(RELEASE_WORKFLOW_PATH.read_text(encoding="utf-8"))]
    joined = "\n".join(runs)
    assert "ghcr.io/" in joined, "the release workflow must push to GHCR"
    assert "tr '[:upper:]' '[:lower:]'" in joined, "GHCR tag must be lowercased"
    assert "steps.version.outputs.native" in joined, (
        "the smoke must compare the baked version against the git-describe version"
    )
    # The image's CMD binds loopback by default; Docker -p cannot reach a
    # loopback-bound process, so the smoke must bind 0.0.0.0 (compose precedent).
    assert "-e CRAWLUTRON_HOST=0.0.0.0" in joined, (
        "the smoke must set CRAWLUTRON_HOST=0.0.0.0 for -p to reach the service"
    )


def test_ci_regenerates_the_version_artifact_before_the_suite() -> None:
    """#949: the version artifact is gitignored, so a CI checkout has none until the
    build backend writes it; the prepare action every gate uses must leave it in
    place."""
    action = PREPARE_ACTION_PATH.read_text(encoding="utf-8")
    assert "scripts/regen_version.py" in action, (
        "the prepare action must regenerate the version artifact before any gate runs"
    )
