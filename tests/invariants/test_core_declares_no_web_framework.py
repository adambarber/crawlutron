"""The engine wheel declares no web framework; the claim is typed and fail-closed."""

from __future__ import annotations

import pathlib

import pytest
from pydantic import ValidationError

from tests._member_manifest import MemberManifest, read_member
from tests.chokepoint_paths import REPO_ROOT

_PACKAGES = REPO_ROOT / "packages"
_FRAMEWORKS = frozenset({"fastapi", "starlette"})


def _member_manifests() -> tuple[MemberManifest, ...]:
    return tuple(read_member(path) for path in sorted(_PACKAGES.glob("*/pyproject.toml")))


def _distribution(requirement: str) -> str:
    return requirement.split("[", 1)[0].split("=", 1)[0].split(">", 1)[0].strip().lower()


def _frameworks(manifest: MemberManifest) -> set[str]:
    return {_distribution(item) for item in manifest.dependencies} & _FRAMEWORKS


def test_core_declares_no_web_framework() -> None:
    core = next(manifest for manifest in _member_manifests() if manifest.name == "crawlutron-core")
    offenders = sorted(_frameworks(core))
    assert not offenders, f"crawlutron-core declares a web framework: {offenders}"


def test_a_planted_framework_is_named(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text('[project]\nname="crawlutron-core"\ndependencies=["fastapi==0.165.0"]\n')
    assert _frameworks(read_member(path)) == {"fastapi"}


@pytest.mark.parametrize("value", ["", "   ", "fastapi extra"])
def test_a_malformed_requirement_is_refused(value: str) -> None:
    with pytest.raises(ValidationError, match="one trimmed token"):
        MemberManifest.create({"name": "crawlutron-core", "dependencies": [value]})


def test_an_unknown_manifest_key_is_refused() -> None:
    with pytest.raises(ValidationError, match="extra"):
        MemberManifest.create({"name": "crawlutron-core", "invented": True})


def test_admit_or_none_collapses_only_refusals() -> None:
    assert MemberManifest.admit_or_none({"name": "crawlutron-core", "invented": True}) is None
    assert MemberManifest.admit_or_none({"name": "crawlutron-core"}) is not None
