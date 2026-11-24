"""Stateless deterministic job identity for retry-safe POSTs (#1152).

One HMAC handle per token + caller + payload; the row PK dedupes retries.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping

from pydantic import BaseModel

from crawlutron.models.caller_identity import CallerIdentity
from crawlutron.safety.json_safe import json_default
from crawlutron.storage.ids import REQUEST_ID_PREFIX

IDEMPOTENCY_HEADER = "Idempotency-Key"
MAX_TOKEN_LENGTH = 256

_ANONYMOUS_KEY = "anon"
_KEYED_PREFIX = "key:"


def _canonical(payload: BaseModel | Mapping[str, object]) -> str:
    """Canonical JSON of every request field; no field here is volatile."""
    data = payload if isinstance(payload, Mapping) else payload.model_dump(mode="json")
    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=json_default)


def derive_request_id(
    token: str,
    caller: CallerIdentity,
    payload: BaseModel | Mapping[str, object],
    secret: str,
) -> str:
    """HMAC-derived job handle: same token, caller and payload reuse one row."""
    caller_key = f"{_KEYED_PREFIX}{caller.caller_id}" if caller.authenticated else _ANONYMOUS_KEY
    key = f"{secret}\x00{caller_key}".encode()
    message = f"{token}\x00{_canonical(payload)}".encode()
    return f"{REQUEST_ID_PREFIX}{hmac.new(key, message, hashlib.sha256).hexdigest()[:32]}"
