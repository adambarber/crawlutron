from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class CallerKey(BaseModel):
    """An inbound caller key record: the stored hash, label, and quotas.

    The secret itself is never stored; verification hashes what the caller presents.
    """

    model_config = ConfigDict(extra="ignore")

    key_id: str
    label: str = ""
    key_hash: str
    max_pages: int = 0
    max_concurrency: int = 0
    created_at: float = 0.0
