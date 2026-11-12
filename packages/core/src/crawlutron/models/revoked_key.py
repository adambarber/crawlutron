"""A revoked key's acknowledgement: idempotent status plus the key id."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class RevokedKey(BaseModel):
    """The response body for a successful key revocation."""

    model_config = ConfigDict(extra="ignore")

    status: str = "revoked"
    key_id: str
