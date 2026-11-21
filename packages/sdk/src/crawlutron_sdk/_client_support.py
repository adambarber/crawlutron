"""Response and query helpers shared by the sync and async SDK facades."""

from __future__ import annotations

import httpx


def str_key_dict(data: object) -> dict[str, object]:
    """Coerce a JSON object to string keys without changing its values."""
    if not isinstance(data, dict):
        return {}
    return {str(key): value for key, value in data.items()}


def json_body(response: httpx.Response) -> dict[str, object]:
    """Decode a response body; a non-JSON error body keeps its HTTP status."""
    if not response.content:
        return {}
    try:
        data = response.json()
    except ValueError:
        return {}
    return str_key_dict(data)


def retry_wait(response: httpx.Response) -> float:
    """Clamp a numeric Retry-After header to [0, 30] seconds; default 2."""
    retry_after = response.headers.get("Retry-After")
    try:
        wait_seconds = float(retry_after) if retry_after is not None else 2.0
    except ValueError:
        wait_seconds = 2.0
    return min(wait_seconds, 30.0)


def payload_dict(payload: object) -> dict[str, object]:
    """Serialize a pydantic model to a plain JSON-ready dictionary."""
    dump = getattr(payload, "model_dump", None)
    if callable(dump):
        return str_key_dict(dump())
    return {}


def job_list_query(limit: int | None, offset: int) -> str:
    """Build the optional limit/offset query for a job listing."""
    parts = [f"limit={limit}"] if limit is not None else []
    parts += [f"offset={offset}"] if offset else []
    return f"?{'&'.join(parts)}" if parts else ""
