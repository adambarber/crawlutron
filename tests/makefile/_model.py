"""The single typed projection of the Makefile, parsed once into strict objects."""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
)

STRICT = ConfigDict(extra="forbid", frozen=True)

_VARIABLE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*([:=?]=)\s*(.*)$")
_TARGET = re.compile(r"^([A-Za-z0-9_-]+(?:\s+[A-Za-z0-9_-]+)*):(.*)$")


class Variable(BaseModel):
    """One Makefile variable assignment: name, operator, value, source line."""

    model_config = STRICT
    name: str = Field(min_length=1)
    operator: str = Field(min_length=1)
    value: str
    line: int


class RecipeLine(BaseModel):
    """One recipe line: the command text and its source line."""

    model_config = STRICT
    command: str = Field(min_length=1)
    line: int


class Target(BaseModel):
    """One Makefile target: name, recipe lines, source line."""

    model_config = STRICT
    name: str = Field(min_length=1)
    recipe: tuple[RecipeLine, ...] = ()
    line: int


class Makefile(BaseModel):
    """The whole Makefile: variables and targets, each carrying source lines."""

    model_config = STRICT
    path: Path
    variables: tuple[Variable, ...] = ()
    targets: tuple[Target, ...] = ()


def parse_makefile(path: Path) -> Makefile:
    """Parse the Makefile once into typed objects; never regex over its text in rules."""
    text = path.read_text(encoding="utf-8")
    variables: list[Variable] = []
    targets: list[Target] = []
    current_target: str | None = None
    current_line = 0
    for number, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if raw[0] not in " \t":
            m = _VARIABLE.match(stripped)
            if m:
                variables.append(
                    Variable(name=m.group(1), operator=m.group(2), value=m.group(3), line=number)
                )
                continue
            m = _TARGET.match(stripped)
            if m:
                current_target = m.group(1)
                current_line = number
                targets.append(Target(name=current_target, recipe=(), line=current_line))
                continue
            continue
        if raw.startswith("\t") and current_target is not None:
            targets[-1] = targets[-1].model_copy(
                update={"recipe": (*targets[-1].recipe, RecipeLine(command=stripped, line=number))}
            )
    return Makefile(path=path, variables=tuple(variables), targets=tuple(targets))
