"""Typed facts parsed once from a step's shell and expressions."""

from __future__ import annotations

import re
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field

from tests.workflows._admission import admit_or_none as _admit_or_none

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
    def create(cls, raw: object, *, context: Mapping[str, object] | None = None) -> ExpressionFacts:
        if not isinstance(raw, str):
            raise ValueError("invalid_expression")
        return cls.parse(raw)

    @classmethod
    def admit_or_none(
        cls, raw: object, *, context: Mapping[str, object] | None = None
    ) -> ExpressionFacts | None:
        return _admit_or_none(lambda value: cls.create(value), raw)

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
    """Quoted spans removed; only the matching quote closes, escapes honoured."""
    kept: list[str] = []
    quote = ""
    escaped = False
    for char in text:
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in "'\"":
            quote = char
            continue
        kept.append(char)
    return "".join(kept)


def _first_open(text: str) -> int:
    """Index of the first unquoted '('; the same scanner as _unquoted."""
    quote = ""
    escaped = False
    for index, char in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in "'\"":
            quote = char
            continue
        if char == "(":
            return index
    return -1


def _export_value(text: str) -> str:
    value = text.strip()
    if not value:
        return ""
    if value[0] in "'\"":
        quote = value[0]
        end = value.find(quote, 1)
        return value[1:end] if end > 0 else value[1:]
    token: list[str] = []
    escaped = False
    for char in value:
        if escaped:
            token.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char.isspace():
            break
        else:
            token.append(char)
    return "".join(token)


class CommandFacts(BaseModel):
    model_config = ConfigDict(frozen=True)
    source: str = ""
    segments: tuple[str, ...] = Field(default_factory=tuple)
    pytest_invocations: tuple[Invocation, ...] = Field(default_factory=tuple)
    pip_install: bool = False
    heredoc: bool = False

    @classmethod
    def create(cls, raw: object, *, context: Mapping[str, object] | None = None) -> CommandFacts:
        if not isinstance(raw, str):
            raise ValueError("invalid_command")
        inherited = (context or {}).get("env")
        env = inherited if isinstance(inherited, Mapping) else None
        return cls.parse(raw, env)

    @classmethod
    def admit_or_none(
        cls, raw: object, *, context: Mapping[str, object] | None = None
    ) -> CommandFacts | None:
        return _admit_or_none(lambda value: cls.create(value, context=context), raw)

    @classmethod
    def parse(cls, run: str, env: Mapping[str, object] | None = None) -> CommandFacts:
        flat = re.sub(r"\\\s*\n\s*", " ", run)
        segments = tuple(part for part in _SEGMENT.split(flat) if part.strip())
        scopes: list[dict[str, str]] = [{key: str(value) for key, value in (env or {}).items()}]
        invocations: list[Invocation] = []
        for segment in segments:
            command = segment.strip()
            scope_view = _unquoted(command)
            opens, closes = scope_view.count("("), scope_view.count(")")
            opener = _first_open(command)
            if opener >= 0:
                scopes.extend(dict(scopes[-1]) for _ in range(opens))
            body = command[opener + 1 :] if opener >= 0 else command
            body = body.strip()
            if body.startswith(("export ", "unset ")):
                export = _EXPORT.search(body)
                unset = _UNSET.search(body)
                if export:
                    scopes[-1][export.group(1)] = _export_value(export.group(2))
                if unset:
                    scopes[-1].pop(unset.group(1), None)
            if "pytest" in segment:
                visible: dict[str, str] = {}
                for frame in scopes:
                    visible.update(frame)
                filtered = has_marker_filter(segment) or any(
                    key in PYTEST_ENV_KEYS and has_marker_filter(value) for key, value in visible.items()
                )
                invocations.append(Invocation(text=command, filtered=filtered))
            for _ in range(closes):
                if len(scopes) > 1:
                    scopes.pop()
        pip = any(_PIP.search(_unquoted(part)) and _INSTALL.search(_unquoted(part)) for part in segments)
        return cls(
            source=run,
            segments=segments,
            pytest_invocations=tuple(invocations),
            pip_install=pip,
            heredoc=bool(_HEREDOC.search(flat)),
        )
