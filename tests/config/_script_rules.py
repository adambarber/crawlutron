"""Script-structure rules over the AST-derived typed facts."""

from __future__ import annotations

from collections.abc import Iterator

from tests.config._bundle import Bundle, _finding
from tests.config._policy import Finding, RuleId


def conftest_bytecode_enabled(bundle: Bundle) -> Iterator[Finding]:
    """conftest must never disable bytecode writes for the xdist fleet."""
    for fact in bundle.conftest.facts:
        if fact.kind == "env-key" and fact.name == "PYTHONDONTWRITEBYTECODE":
            yield _finding(
                RuleId.CONFTEST_BYTECODE_ENABLED,
                bundle.conftest.path.name,
                fact.line,
                "conftest disables bytecode writes via PYTHONDONTWRITEBYTECODE",
            )


def image_release_script(bundle: Bundle) -> Iterator[Finding]:
    """The image script binds the smoke to a reachable host, its baked version, and its baked proxy."""
    image = bundle.image
    for value in (
        "CRAWLUTRON_HOST=0.0.0.0",
        "ghcr.io",
        "docker",
        "push",
        "CRAWLUTRON_EGRESS_PROXY_URL=",
    ):
        if not image.has_string(value):
            yield _finding(
                RuleId.IMAGE_RELEASE_SCRIPT, image.path.name, 1, f"image script lost the {value!r} contract"
            )
    functions = {fact.name for fact in image.of_kind("function")}
    if "_check_baked_version" not in functions:
        yield _finding(
            RuleId.IMAGE_RELEASE_SCRIPT, image.path.name, 1, "image script must check the baked version"
        )
    if "_check_baked_proxy" not in functions:
        yield _finding(
            RuleId.IMAGE_RELEASE_SCRIPT, image.path.name, 1, "image script must check the baked proxy"
        )
    if not image.has_compare("baked_version", "!=", "native_version"):
        yield _finding(
            RuleId.IMAGE_RELEASE_SCRIPT,
            image.path.name,
            1,
            "baked version must be compared with the tag version",
        )
    if not any(fact.name == "lower" for fact in image.of_kind("call")):
        yield _finding(RuleId.IMAGE_RELEASE_SCRIPT, image.path.name, 1, "a tag must be lowercased before use")
    # Verify _smoke_image calls _check_baked_proxy
    calls = {fact.name for fact in image.of_kind("call")}
    if "_check_baked_proxy" not in calls:
        yield _finding(
            RuleId.IMAGE_RELEASE_SCRIPT,
            image.path.name,
            1,
            "_smoke_image must call _check_baked_proxy",
        )
