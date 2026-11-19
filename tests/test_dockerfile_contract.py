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
