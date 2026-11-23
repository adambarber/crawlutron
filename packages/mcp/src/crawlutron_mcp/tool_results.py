"""The MCP tool-result seam: one typed refusal becomes one structured tool error."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
from typing import cast

from pydantic import BaseModel, ValidationError

from crawlutron.models.json_value import JsonValue
from crawlutron.safety.json_safe import json_default
from crawlutron.service.error_rendering import ErrorDoor, redact_urls_in_text, render_error
from crawlutron.service.errors import DomainError


@dataclass(frozen=True)
class ToolResult:
    """A ``tools/call`` outcome: text content plus optional structured data."""

    text: str
    structured: JsonValue | None = None
    is_error: bool = False


def validation_message(error: Exception) -> str:
    if isinstance(error, ValidationError):
        text = "; ".join(
            f"{'.'.join(str(part) for part in err.get('loc', []))}: {err.get('msg', '')}"
            for err in error.errors()
        )
    else:
        text = str(error)
    return str(redact_urls_in_text(text))


def tool_error_result(error: DomainError) -> ToolResult:
    """Render one typed refusal into the tool-error body."""
    rendered = render_error(error, ErrorDoor.MCP)
    return ToolResult(
        text=str(rendered.message), structured=cast("JsonValue", rendered.envelope), is_error=True
    )


def _tool_error(error: ValueError | DomainError) -> ToolResult:
    if isinstance(error, DomainError):
        return tool_error_result(error)
    return ToolResult(text=str(error), is_error=True)


def tool_success_result(value: BaseModel | Mapping[str, object]) -> ToolResult:
    # Validated boundary: a model dump is JSON by construction; a caller's mapping
    # crossing the wire is the JSON values it carries.
    dump = value.model_dump() if isinstance(value, BaseModel) else cast("JsonValue", dict(value))
    return ToolResult(text=json.dumps(dump, default=json_default), structured=dump)


async def tool_result(coro: Awaitable[BaseModel | Mapping[str, object]]) -> ToolResult:
    """Cross the MCP boundary for one tool call: run it, or turn the endpoint's
    ``DomainError``/``ValueError`` into a tool error rather than a traceback.
    """
    try:
        result = await coro
    except (DomainError, ValueError) as error:
        return _tool_error(error)
    return tool_success_result(result)


__all__ = ["ToolResult", "tool_error_result", "tool_result", "tool_success_result", "validation_message"]
