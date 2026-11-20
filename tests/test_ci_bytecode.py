"""Bytecode and version consistency tests for CI."""

from __future__ import annotations

import os

from tests._ci_contract import RELEASE_WORKFLOW_PATH, WORKFLOW_PATH, _job_run_steps, job_steps


def test_conftest_leaves_bytecode_writing_enabled() -> None:
    """S4: conftest must not set PYTHONDONTWRITEBYTECODE so modules compile to
    .pyc once and are reused across xdist workers and runs."""
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
    import yaml

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
