"""The Dockerfile's offline-seed contract: the seed context is optional for
offline builds; bare `docker build .` fetches the browser over the wire."""

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
    """Compose may declare the optional ``seed`` context for offline builds;
    bare ``docker build .`` fetches the browser over the wire without it.
    """
    document = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    assert isinstance(document, dict), "compose must be a mapping"
    build = document["services"]["crawlutron"]["build"]
    contexts = build.get("additional_contexts")
    assert isinstance(contexts, list), (
        "compose build must declare additional_contexts: a build without the "
        "seed context fetches the browser over the wire instead"
    )
    assert any(str(c).startswith("seed=") for c in contexts), (
        f"compose must supply the seed context, found {contexts!r}"
    )


def test_dockerfile_builds_bare_without_seed_context() -> None:
    """Bare ``docker build .`` must succeed from a clean checkout: camoufox
    fetches over the wire at build time and the image needs no ``seed``
    build context (BuildKit would otherwise pull docker.io/library/seed).
    """
    text = DOCKERFILE_PATH.read_text(encoding="utf-8")
    assert "python -m camoufox fetch" in text, "camoufox fetch must run at build time"
    assert "type=bind,from=seed,target=/seed" not in text, (
        "no mandatory seed bind mount: bare docker build . must not need --build-context seed="
    )
