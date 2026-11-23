"""One typed refusal rendered for every door: status/code/message/details.

The renderer is the only place that reads a ``DomainError`` triplet, applies URL
redaction, and builds the wire envelope the REST/MCP/CLI/SDK doors all carry.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum, auto
from typing import cast

from crawlutron.models.error_response import ErrorEnvelope
from crawlutron.models.json_value import JsonValue
from crawlutron.service.errors import DomainError
from safe_url.url_redaction import redact_urls_in_text


class ErrorDoor(StrEnum):
    """The four surfaces that render one typed refusal."""

    REST = auto()
    MCP = auto()
    CLI = auto()
    SDK = auto()


class MachineCode(StrEnum):
    """Non-reflecting codes for protocol-level refusals with no DomainError."""

    UNKNOWN_TOOL = "unknown_tool"
    UNKNOWN_METHOD = "unknown_method"
    HEADER_MISMATCH = "header_mismatch"


def error_envelope(code: str, message: str, details: JsonValue | None = None) -> ErrorEnvelope:
    """The single definition of the wire error shape; details is always present."""
    return {"code": code, "message": message, "details": details or {}}


@dataclass(frozen=True)
class RenderedError:
    """One refusal's redacted triplet, its envelope, and its door-specific body."""

    status: int
    code: str
    message: str
    details: JsonValue
    envelope: ErrorEnvelope
    wire: JsonValue


def _wire_value(value: JsonValue) -> JsonValue:
    """Map every string in a payload through the one redaction chokepoint."""
    if isinstance(value, str):
        return redact_urls_in_text(value)
    if isinstance(value, list):
        return [_wire_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _wire_value(item) for key, item in value.items()}
    return value


def render_error(error: DomainError, door: ErrorDoor) -> RenderedError:
    """Read the triplet, redact it, and emit one door's wire shape."""
    message = str(_wire_value(error.message))
    details = _wire_value(error.details) if error.details is not None else {}
    envelope = error_envelope(error.code, message, details)
    # The TypedDict is the wire contract; JsonValue is its closed recursive form.
    wire: JsonValue = cast("JsonValue", envelope)
    if door is ErrorDoor.MCP:
        wire = {
            "resultType": "complete",
            "content": [{"type": "text", "text": message}],
            "isError": True,
            "structuredContent": cast("JsonValue", envelope),
        }
    return RenderedError(error.status, error.code, message, details, envelope, wire)


__all__ = [
    "ErrorDoor",
    "MachineCode",
    "RenderedError",
    "error_envelope",
    "redact_urls_in_text",
    "render_error",
]
