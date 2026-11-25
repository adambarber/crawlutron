"""Bounded JSON admit/refuse door for crawlutron_extract.

Extract-local chokepoint: cap bytes, bound depth, enforce shape, refuse safely.
"""

from __future__ import annotations

import json

#: Cap on admitted JSON input bytes; crawled content is bounded before it ever
#: reaches ``json.loads``.
MAX_JSON_INPUT_BYTES = 1_000_000

#: Max admitted nesting depth of a parsed JSON value.
MAX_JSON_DEPTH = 50


def _json_depth(obj: object, depth: int = 0) -> int:
    """Nesting depth of a parsed JSON value; a scalar or empty container is 0.

    Recursive; the byte cap above bounds admitted size, so recursion depth stays
    within the admitted nesting bound and cannot blow the stack.
    """
    if isinstance(obj, dict):
        return max((_json_depth(value, depth + 1) for value in obj.values()), default=depth)
    if isinstance(obj, list):
        return max((_json_depth(value, depth + 1) for value in obj), default=depth)
    return depth


def parse_json(
    text: object,
    *,
    max_bytes: int = MAX_JSON_INPUT_BYTES,
    max_depth: int = MAX_JSON_DEPTH,
    expected: type | tuple[type, ...] | None = None,
) -> object:
    """Admit a JSON value, bounded by bytes and depth.

    Refuses non-str, oversized, malformed, wrong-shape, or over-deep JSON.
    """
    if not isinstance(text, str):
        raise ValueError(f"json input must be str, got {type(text).__name__}")
    if len(text.encode("utf-8")) > max_bytes:
        raise ValueError("json input exceeds byte cap")
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError, RecursionError) as error:
        raise ValueError("malformed json") from error
    if expected is not None and not isinstance(data, expected):
        raise ValueError(f"json root must be {expected}, got {type(data).__name__}")
    if _json_depth(data) > max_depth:
        raise ValueError(f"json depth exceeds max_depth={max_depth}")
    return data
