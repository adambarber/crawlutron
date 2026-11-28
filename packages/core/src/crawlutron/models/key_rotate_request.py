"""The operator-supplied replacement secret for a key rotation."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class KeyRotateRequest(BaseModel):
    """The new ``ck_<id>.<body>`` secret: typed SecretStr, repr-hidden,
    bounded to 512 characters and refused when empty."""

    model_config = ConfigDict(extra="forbid")

    new_secret: Annotated[SecretStr, Field(repr=False, min_length=1, max_length=512)]
