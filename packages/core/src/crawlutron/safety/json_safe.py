"""JSON door: one canonical rendering and one canonical parse.

Serialisation had a door; parsing did not -- ``parse_json`` closes the funnel.
"""

from __future__ import annotations

import datetime
import json

from crawlutron.models.json_value import JsonValue
from safe_url.url_redaction import redacted_marker


class ParseRefused(Exception):
    """A refused JSON parse. The message names the refusal class only -- it never
    echoes the attacker-controlled payload, so this door is safe to surface in
    logs, envelopes, and client errors.
    """


#: One JSON byte cap, mirroring bounded_io's single-cap-per-input discipline: a
#: hostile body is refused before it is parsed, never buffered in full.
MAX_JSON_INPUT_BYTES = 4 * 1024 * 1024


def _expected_name(expected: type | tuple[type, ...]) -> str:
    """A human name for the admitted type, used only in refusal messages."""
    if isinstance(expected, tuple):
        return " or ".join(getattr(item, "__name__", str(item)) for item in expected)
    return getattr(expected, "__name__", str(expected))


def _json_depth(text: str) -> int:
    """Maximum container-nesting depth, ignoring brackets inside strings."""
    depth = 0
    max_depth = 0
    in_string = False
    escape = False
    for char in text:
        if escape:
            escape = False
            continue
        if in_string:
            if char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char in "{[":
            depth += 1
            if depth > max_depth:
                max_depth = depth
        elif char in "}]":
            depth -= 1
    return max_depth


def parse_json(text: object, *, expected: type | tuple[type, ...], max_depth: int) -> JsonValue:
    """Admit-or-refuse JSON door: cap bytes, bound depth, enforce type, refuse."""
    if not isinstance(text, str):
        raise ParseRefused("JSON input must be str, not bytes")
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_JSON_INPUT_BYTES:
        raise ParseRefused("JSON payload exceeds byte cap")
    if _json_depth(text) > max_depth:
        raise ParseRefused("JSON depth exceeds max_depth")
    try:
        payload: JsonValue = json.JSONDecoder().decode(text)
    except json.JSONDecodeError as error:
        raise ParseRefused("JSON payload is not valid JSON") from error
    if not isinstance(payload, expected):
        raise ParseRefused(f"JSON payload is not a {_expected_name(expected)}")
    return payload


def json_default(obj: object) -> object:
    """Fallback for objects that are not natively JSON-serializable."""
    # Secrets never surface through a generic serializer: redaction is the exit law.
    getter = getattr(obj, "get_secret_value", None)
    if callable(getter):
        return redacted_marker()
    reducer = getattr(obj, "model_dump", None)
    if callable(reducer):
        return reducer()
    reducer = getattr(obj, "dict", None)
    if callable(reducer):
        return reducer()
    if isinstance(obj, (datetime.datetime, datetime.date)):
        return obj.isoformat()
    if isinstance(obj, bytes):
        return obj.decode("utf-8", "replace")
    if isinstance(obj, (set, frozenset)):
        return list(obj)
    if isinstance(obj, Exception):
        return f"{type(obj).__name__}: {obj}"
    if hasattr(obj, "__dict__"):
        return {k: v for k, v in vars(obj).items() if not k.startswith("_")}
    return str(obj)
