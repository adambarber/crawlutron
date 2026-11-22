"""Typed facts parsed once from a step's shell and expressions."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field

_SEGMENT = re.compile(r"&&|;|\||\n")
_EXPRESSION = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)
_EVENT_REF = re.compile(r"github(?:\.event|\['event'\])", re.IGNORECASE)
_MARKER = re.compile(r"(?:^|[\s'\"])-m[= ]+['\"]?([^\s'\"]+)")
_PIP = re.compile(r"\bpip(?:3(?:\.\d+)?)?\b")
_INSTALL = re.compile(r"\binstall\b(?![-\w])")
_HEREDOC = re.compile(r"<<-?\s*['\"]?[A-Za-z_]")
PYTEST_ENV_KEYS = frozenset({"PYTEST_ADDOPTS", "PYTEST_ARGS"})


def has_marker_filter(text: str) -> bool:
    """A pytest -m filter, never python's -m module invocation."""
    return any(match.group(1).lower() != "pytest" for match in _MARKER.finditer(text))


class ExpressionFacts(BaseModel):
    model_config = ConfigDict(frozen=True)
    source: str
    references_event: bool

    @classmethod
    def parse(cls, text: str) -> ExpressionFacts:
        bodies = _EXPRESSION.findall(text)
        return cls(
            source=text,
            references_event=any(
                _EVENT_REF.search(re.sub(r"\s+", "", body).replace('"', "'")) for body in bodies
            ),
        )


class CommandFacts(BaseModel):
    model_config = ConfigDict(frozen=True)
    source: str
    segments: tuple[str, ...] = Field(default_factory=tuple)
    marker_filter: bool = False
    pip_install: bool = False
    heredoc: bool = False

    @classmethod
    def parse(cls, run: str) -> CommandFacts:
        flat = re.sub(r"\\\s*\n\s*", " ", run)
        segments = tuple(part for part in _SEGMENT.split(flat) if part.strip())
        marker = False
        pip = False
        for segment in segments:
            has_pytest = "pytest" in segment
            if has_pytest and has_marker_filter(segment):
                marker = True
            if _PIP.search(segment) and _INSTALL.search(segment):
                pip = True
        return cls(
            source=run,
            segments=segments,
            marker_filter=marker,
            pip_install=pip,
            heredoc=bool(_HEREDOC.search(flat)),
        )
