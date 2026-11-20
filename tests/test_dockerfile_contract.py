"""The Dockerfile's build contract: the venv is the image's only source tree,
and bare `docker build .` fetches the browser over the wire."""

from __future__ import annotations

from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parents[1]

DOCKERFILE_PATH = SERVICE_ROOT / "Dockerfile"


def _stages() -> list[str]:
    return DOCKERFILE_PATH.read_text(encoding="utf-8").split("\nFROM ")


def _stage(alias: str) -> str:
    """One stage's text, found by the alias it declares."""
    for stage in _stages():
        if stage.splitlines() and stage.splitlines()[0].rstrip().endswith(f"AS {alias}"):
            return stage
    raise AssertionError(f"the Dockerfile declares no {alias} stage")


def _syncing_stages() -> list[str]:
    stages = [stage for stage in _stages() if "uv sync --locked" in stage]
    assert stages, "no stage runs uv sync --locked; the gate would pass vacuously"
    return stages


def test_base_image_is_digest_pinned_3_14() -> None:
    """The 3.14 base must be pinned by tag AND digest, so a rebuild is
    byte-for-byte reproducible and the major version is explicit (#625).
    """
    text = DOCKERFILE_PATH.read_text(encoding="utf-8")
    arg = [line for line in text.splitlines() if line.startswith("ARG CRAWLUTRON_BASE_IMAGE=")]
    assert len(arg) == 1, "exactly one CRAWLUTRON_BASE_IMAGE ARG must pin the base"
    value = arg[0].split("=", 1)[1]
    assert "python:3.14" in value, f"base must be a 3.14 tag: {value!r}"
    assert "@sha256:" in value, f"base must be digest-pinned: {value!r}"


def test_the_runtime_stage_takes_its_source_from_the_venv() -> None:
    """#1171: a hand-kept COPY list is how safe_url and crawlutron_extract went
    missing from the image. The builder installs every member as a wheel, so the
    runtime stage owns no source tree at all.
    """
    assert "--no-editable" in _stage("builder"), "the builder must install members as wheels"
    copied = [
        line for line in _stage("runtime").splitlines() if line.startswith("COPY") and "packages" in line
    ]
    assert not copied, f"the runtime stage copies source trees the venv already carries: {copied}"
    assert "PYTHONPATH=/app" not in _stage("runtime"), (
        "the runtime stage must import from the venv, not from a source tree at /app"
    )


def test_dockerfile_fetches_the_browser_without_a_build_context() -> None:
    """Bare ``docker build .`` must succeed from a clean checkout: camoufox
    fetches over the wire at build time and the image needs no extra context.
    """
    text = DOCKERFILE_PATH.read_text(encoding="utf-8")
    assert "python -m camoufox fetch" in text, "camoufox fetch must run at build time"
    assert "from=seed" not in text, "no seed bind mount: bare docker build . must need no build context"


def test_every_stage_that_syncs_the_workspace_first_copies_the_members() -> None:
    """`uv sync --locked` resolves the workspace members, so a stage must copy them.

    A stage holding only `pyproject.toml` and `uv.lock` dies at "Distribution not
    found at: file:///app/packages/core".
    """
    for stage in _syncing_stages():
        copy = next(line for line in stage.splitlines() if line.startswith("COPY "))
        assert any(line.startswith("COPY ") and "packages" in line for line in stage.splitlines()), (
            "a stage runs `uv sync --locked` without copying packages/: the workspace "
            f"members it resolves live there, and the first COPY is {copy!r}"
        )


def test_runtime_command_supplies_uvicorn_defaults() -> None:
    """Empty env expansions make uvicorn exit before the health probe."""
    text = DOCKERFILE_PATH.read_text(encoding="utf-8")
    assert "--workers ${CRAWLUTRON_WORKERS:-1}" in text
    assert "--log-level ${CRAWLUTRON_LOG_LEVEL:-info}" in text


def test_the_fpgen_model_is_installed_without_the_github_api() -> None:
    """The REST fetch rate-limits repeated builds; the pinned URL does not."""
    text = DOCKERFILE_PATH.read_text(encoding="utf-8")
    assert text.count("--mount=type=secret,id=github_token") == 1
    assert text.count('export GITHUB_TOKEN="$(cat /run/secrets/github_token)"') == 1
    assert "python -m fpgen fetch" not in text
    assert "ensure_fpgen_model()" in text


def test_image_builds_pass_the_token_into_the_secret_mount() -> None:
    """A Dockerfile secret is only usable when the workflow supplies it."""
    import yaml

    for name in ("packaging.yml", "release.yml"):
        path = SERVICE_ROOT / ".github/workflows" / name
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(document, dict)
        jobs = document.get("jobs")
        assert isinstance(jobs, dict)
        builds = [
            step
            for job in jobs.values()
            if isinstance(job, dict) and isinstance(job.get("steps"), list)
            for step in job["steps"]
            if isinstance(step, dict) and "docker/build-push-action" in str(step.get("uses", ""))
        ]
        assert builds, f"{name} has no image build step"
        for step in builds:
            with_block = step.get("with")
            assert isinstance(with_block, dict), f"{name} has no build inputs"
            assert "github_token=${{ secrets.GITHUB_TOKEN }}" in str(with_block.get("secrets", "")), name
