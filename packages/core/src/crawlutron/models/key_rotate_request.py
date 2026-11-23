"""The operator-supplied replacement secret for a key rotation."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class KeyRotateRequest(BaseModel):
    """The new ``ck_<id>.<body>`` secret; shape is validated by the store."""

    model_config = ConfigDict(extra="forbid")

    new_secret: str
