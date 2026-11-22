"""Strict typed projection of a workspace member manifest."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Annotated, Self

from pydantic import AfterValidator, BaseModel, ConfigDict

from tests.workflows._admission import admit_or_none as _admit_or_none

STRICT = ConfigDict(extra="forbid", frozen=True)


def _requirement(value: str) -> str:
    if not value or value != value.strip() or len(value.split()) != 1:
        raise ValueError("a requirement must be one trimmed token")
    return value


Requirement = Annotated[str, AfterValidator(_requirement)]


class MemberManifest(BaseModel):
    """A member's declared distribution and runtime requirements."""

    model_config = STRICT
    name: str
    dependencies: tuple[Requirement, ...] = ()

    @classmethod
    def create(cls, raw: object) -> Self:
        return cls.model_validate(raw)

    @classmethod
    def admit_or_none(cls, raw: object) -> Self | None:
        return _admit_or_none(lambda value: cls.create(value), raw)


def read_member(path: Path) -> MemberManifest:
    """Parse one member manifest through the typed door, never a loose dict."""
    project = tomllib.loads(path.read_text(encoding="utf-8")).get("project")
    if not isinstance(project, dict):
        raise ValueError("manifest has no project table")
    return MemberManifest.create(
        {"name": project.get("name"), "dependencies": project.get("dependencies", [])}
    )
