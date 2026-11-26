"""The Dockerfile's build contract: the venv is the image's only source tree,
and bare `docker build .` fetches the browser over the wire."""

from __future__ import annotations

from tests.chokepoint_paths import REPO_ROOT

SERVICE_ROOT = REPO_ROOT

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


def _alias_of(stage: str) -> str | None:
    """The AS name of a stage chunk, or None for the pre-FROM header."""
    lines = stage.splitlines()
    if not lines or " AS " not in lines[0]:
        return None
    return lines[0].rsplit(" AS ", 1)[1].strip()


def _default_stage() -> str:
    """The last stage, which `docker build .` produces when no --target is given."""
    return _stages()[-1]


def _effective_text(alias: str, stages: dict[str, str]) -> str:
    """A stage plus every earlier stage it derives from, following FROM bases."""
    parts: list[str] = []
    seen: set[str] = set()
    pending = [alias]
    while pending:
        current = pending.pop()
        if current in seen or current not in stages:
            continue
        seen.add(current)
        stage = stages[current]
        parts.append(stage)
        head = stage.splitlines()[0].split(" AS ")[0].strip()
        if head and not head.startswith("${"):
            pending.append(head)
    return "\n".join(parts)


def test_base_image_is_digest_pinned_3_12() -> None:
    """The 3.12 base must be pinned by tag AND digest, so a rebuild is
    byte-for-byte reproducible and the major version is explicit (#625).
    """
    text = DOCKERFILE_PATH.read_text(encoding="utf-8")
    arg = [line for line in text.splitlines() if line.startswith("ARG CRAWLUTRON_BASE_IMAGE=")]
    assert len(arg) == 1, "exactly one CRAWLUTRON_BASE_IMAGE ARG must pin the base"
    value = arg[0].split("=", 1)[1]
    assert "python:3.12" in value, f"base must be a 3.12 tag: {value!r}"
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


def test_dockerfile_fetches_the_browser_without_a_seed_mount() -> None:
    """A bare checkout builds: the pinned installer ships in the context, no seed mount."""
    text = DOCKERFILE_PATH.read_text(encoding="utf-8")
    assert "python -m camoufox_offline_fetch" in text, "the pinned installer must run at build time"
    assert "COPY scripts/camoufox_offline_fetch.py" in text
    assert "from=seed" not in text, "no seed bind mount: bare docker build . must need no seed"


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


def test_builder_installs_root_so_the_image_reports_the_release_version() -> None:
    """Without the root dist, crawlutron.__version__ reads the core member's version."""
    text = DOCKERFILE_PATH.read_text(encoding="utf-8")
    assert "COPY pyproject.toml uv.lock README.md LICENSE ./" in text
    assert "uv sync --locked --no-editable --python /usr/local/bin/python" in text
    ignore = (DOCKERFILE_PATH.parent / ".dockerignore").read_text(encoding="utf-8")
    assert "!README.md" in ignore and "!LICENSE" in ignore


def test_runtime_command_supplies_uvicorn_defaults() -> None:
    """Empty env expansions make uvicorn exit before the health probe."""
    text = DOCKERFILE_PATH.read_text(encoding="utf-8")
    assert "--workers ${CRAWLUTRON_WORKERS:-1}" in text
    assert "--log-level ${CRAWLUTRON_LOG_LEVEL:-info}" in text


def test_no_image_build_receives_a_credential() -> None:
    """Neither camoufox nor fpgen needs a token; unreadable mounts broke builds."""
    text = DOCKERFILE_PATH.read_text(encoding="utf-8")
    assert "--mount=type=secret" not in text
    assert "GITHUB_TOKEN" not in text
    assert "python -m fpgen fetch" not in text
    assert "python -m camoufox fetch" not in text
    assert "python -m camoufox_offline_fetch" in text
    assert "ensure_fpgen_model()" in text
    ignore = (DOCKERFILE_PATH.parent / ".dockerignore").read_text(encoding="utf-8")
    assert "!scripts/camoufox_offline_fetch.py" in ignore


def test_image_builds_supply_no_secret() -> None:
    """A build-time token is unreadable under BuildKit and unnecessary here."""
    import yaml

    for name in ("packaging.yml", "release.yml"):
        path = SERVICE_ROOT / ".github/workflows" / name
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(document, dict)
        jobs = document.get("jobs")
        assert isinstance(jobs, dict)
        # The release workflow now uses a script for building; verify it calls the script
        if name == "release.yml":
            image_steps = [
                step
                for job in jobs.values()
                if isinstance(job, dict) and job.get("name") == "Publish the container image"
                for step in job.get("steps", [])
                if isinstance(step, dict) and "scripts.release.image" in str(step.get("run", ""))
            ]
            assert image_steps, "release.yml must call scripts.release.image"
            # Verify no secrets in the script call
            for step in image_steps:
                assert "secrets" not in step.get("with", {}), f"{name} passes a secret into a build"
        else:
            builds = [
                step
                for job in jobs.values()
                if isinstance(job, dict) and isinstance(job.get("steps"), list)
                for step in job["steps"]
                if isinstance(step, dict) and "docker/build-push-action" in str(step.get("uses", ""))
            ]
            assert builds, f"{name} has no image build step"
            for step in builds:
                assert "secrets" not in step.get("with", {}), f"{name} passes a secret into a build"


def test_default_build_target_is_the_reload_free_runtime() -> None:
    """`docker build .` must ship the production image, not hot-reload dev."""
    stages = {alias: stage for stage in _stages() if (alias := _alias_of(stage))}
    default = _alias_of(_default_stage())
    assert default is not None
    effective = _effective_text(default, stages)
    assert "--reload" not in effective, f"the default stage {default!r} runs uvicorn with --reload"
    assert "--log-level debug" not in effective
    assert "--workers ${CRAWLUTRON_WORKERS:-1}" in effective
    assert "--log-level ${CRAWLUTRON_LOG_LEVEL:-info}" in effective


def test_default_stage_is_named_runtime() -> None:
    """The production stage is last so it is the build default."""
    assert _alias_of(_default_stage()) == "runtime"


def test_development_stage_keeps_hot_reload_as_an_opt_in() -> None:
    """The dev workflow survives via --target development."""
    assert "--reload" in _stage("development")
    assert "--log-level debug" in _stage("development")


def test_compose_builds_the_development_target() -> None:
    """Local development compose opts into the hot-reload target explicitly."""
    import yaml

    document = yaml.safe_load((SERVICE_ROOT / "docker-compose.dev.yml").read_text(encoding="utf-8"))
    assert document["services"]["crawlutron"]["build"]["target"] == "development"
