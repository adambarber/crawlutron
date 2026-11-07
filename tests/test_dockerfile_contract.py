"""The Dockerfile's offline-seed contract: named contexts are mandatory."""

from __future__ import annotations

from pathlib import Path

import yaml

SERVICE_ROOT = Path(__file__).resolve().parents[1]

DOCKERFILE_PATH = SERVICE_ROOT / "Dockerfile"

COMPOSE_PATH = SERVICE_ROOT / "docker-compose.yml"


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


def test_compose_supplies_the_seed_context() -> None:
    """A `--mount=type=bind,from=seed` RUN resolves the named context at build
    start -- absent it, BuildKit tries docker.io/library/seed and the build
    fails before the `if [ -d /seed ]` fallback can ever run (probed live,
    #625). Compose must always declare the context; bare builds pass
    --build-context pointing at any dir, empty or a real cache.
    """
    document = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    assert isinstance(document, dict), "compose must be a mapping"
    build = document["services"]["crawlutron"]["build"]
    contexts = build.get("additional_contexts")
    assert isinstance(contexts, list), (
        "compose build must declare additional_contexts: a build without the "
        "seed context resolves docker.io/library/seed and fails"
    )
    assert any(str(c).startswith("seed=") for c in contexts), (
        f"compose must supply the seed context, found {contexts!r}"
    )


def test_seed_mount_fallback_survives_an_empty_seed() -> None:
    """The RUN's shell must treat an empty seed dir as 'fetch normally': the
    cp guard fails silently and camoufox fetch runs over the wire.
    """
    text = DOCKERFILE_PATH.read_text(encoding="utf-8")
    assert "type=bind,from=seed,target=/seed" in text, "seed bind mount missing"
    assert "if [ -d /seed ]" in text, "seed presence guard missing"
    assert "2>/dev/null" in text, "empty-seed cp failure must be silenced, not fatal"
