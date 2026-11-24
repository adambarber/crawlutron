"""A rotated key's acknowledgement: idempotent status plus the key id."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class RotatedKey(BaseModel):
    """The response body for a successful key rotation; the secret is not echoed."""

    model_config = ConfigDict(extra="ignore")

    status: str = "rotated"
    key_id: str
