"""Shared JSON fallback serializer: one canonical rendering for non-native values.

Three ``default=`` fallbacks drifted: ``JobStore._json_default``
(datetime/bytes/set/Exception specials), ``output_formats._default``
(pydantic/model reducers), and bare ``default=str`` sprinkled through
webhooks/MCP/logging (crudest -- ``str(datetime)`` with a space instead of
ISO-8601). Union semantics below; every ``json.dumps`` with a fallback uses
:func:`json_default` so renderings cannot drift again.
"""

from __future__ import annotations

import datetime
from typing import Any


def json_default(obj: Any) -> Any:
    """Fallback for objects that are not natively JSON-serializable."""
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
