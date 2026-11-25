"""Bounded JSON admit/refuse door for crawlutron_extract.

Extract-local chokepoint: cap bytes, bound depth, enforce shape.
Refusals raise JsonRefused; the message never echoes the payload.
"""

from __future__ import annotations

import json

#: Cap on admitted JSON input bytes; crawled content is bounded before it ever
#: reaches ``json.loads``.
MAX_JSON_INPUT_BYTES = 1_000_000

#: Max admitted nesting depth; the root container counts as depth 1 (core
#: convention), so ``{}`` and ``[]`` are refused at max_depth=0.
MAX_JSON_DEPTH = 50


class JsonRefused(ValueError):
    """A refused JSON parse; the message names the refusal class only."""


def _json_depth(obj: object) -> int:
    """Nesting depth of a parsed JSON value; the root container counts as 1.

    Recursive; the byte cap above bounds admitted size, so recursion depth stays
    within the admitted nesting bound and cannot blow the stack.
    """
    if isinstance(obj, dict):
        return 1 + max((_json_depth(value) for value in obj.values()), default=0)
    if isinstance(obj, list):
        return 1 + max((_json_depth(value) for value in obj), default=0)
    return 0


def parse_json(
    text: object,
    *,
    max_bytes: int = MAX_JSON_INPUT_BYTES,
    max_depth: int = MAX_JSON_DEPTH,
    expected: type | tuple[type, ...] | None = None,
) -> object:
    """Admit a JSON value, bounded by bytes and depth.

    Refuses non-str, oversized, malformed, wrong-shape, or over-deep JSON with
    JsonRefused.
    """
    if not isinstance(text, str):
        raise JsonRefused(f"json input must be str, got {type(text).__name__}")
    if len(text.encode("utf-8")) > max_bytes:
        raise JsonRefused("json input exceeds byte cap")
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError, RecursionError) as error:
        raise JsonRefused("malformed json") from error
    if expected is not None and not isinstance(data, expected):
        raise JsonRefused(f"json root must be {expected}, got {type(data).__name__}")
    if _json_depth(data) > max_depth:
        raise JsonRefused(f"json depth exceeds max_depth={max_depth}")
    return data
