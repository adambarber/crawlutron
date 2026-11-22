"""Typed facts parsed once from a step's shell and expressions."""

from __future__ import annotations

import re
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field

_SEGMENT = re.compile(r"&&|;|\||\n")
_EXPRESSION = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)
_EVENT_REF = re.compile(r"(?<![\w.])github\s*(?:\.\s*event\b|\[\s*['\"]event['\"]\s*\])", re.IGNORECASE)
_MARKER = re.compile(r"(?:^|[\s'\"])-m(?:[= ]+|(?=['\"]))['\"]?([^\s'\"]+)")
_EXPORT = re.compile(r"\bexport\s+([A-Z_][A-Z0-9_]*)=(.*)")
_UNSET = re.compile(r"\bunset\s+([A-Z_][A-Z0-9_]*)\b")
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


class Invocation(BaseModel):
    model_config = ConfigDict(frozen=True)
    text: str
    filtered: bool


def _unquoted(text: str) -> str:
    return re.sub(r"['\"].*?['\"]", "", text)


def _export_value(text: str) -> str:
    value = text.strip()
    if not value:
        return ""
    if value[0] in "'\"":
        quote = value[0]
        end = value.find(quote, 1)
        return value[1:end] if end > 0 else value[1:]
    return value.split()[0]


class CommandFacts(BaseModel):
    model_config = ConfigDict(frozen=True)
    source: str = ""
    segments: tuple[str, ...] = Field(default_factory=tuple)
    pytest_invocations: tuple[Invocation, ...] = Field(default_factory=tuple)
    pip_install: bool = False
    heredoc: bool = False

    @classmethod
    def parse(cls, run: str, env: Mapping[str, object] | None = None) -> CommandFacts:
        flat = re.sub(r"\\\s*\n\s*", " ", run)
        segments = tuple(part for part in _SEGMENT.split(flat) if part.strip())
        state = {key: str(value) for key, value in (env or {}).items()}
        invocations: list[Invocation] = []
        for segment in segments:
            export = _EXPORT.search(segment)
            unset = _UNSET.search(segment)
            if export:
                state[export.group(1)] = _export_value(export.group(2))
            if unset:
                state.pop(unset.group(1), None)
            if "pytest" in segment:
                filtered = has_marker_filter(segment) or any(
                    key in PYTEST_ENV_KEYS and has_marker_filter(value) for key, value in state.items()
                )
                invocations.append(Invocation(text=segment.strip(), filtered=filtered))
        pip = any(_PIP.search(_unquoted(part)) and _INSTALL.search(_unquoted(part)) for part in segments)
        return cls(
            source=run,
            segments=segments,
            pytest_invocations=tuple(invocations),
            pip_install=pip,
            heredoc=bool(_HEREDOC.search(flat)),
        )
