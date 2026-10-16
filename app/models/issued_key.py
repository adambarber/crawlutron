from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class IssuedKey(BaseModel):
    """A newly issued key: the only moment the secret is visible.

    The secret is shown once at issuance and never stored; everything else mirrors the record.
    """

    model_config = ConfigDict(extra="ignore")

    key_id: str
    label: str = ""
    secret: str
    max_pages: int = 0
    max_concurrency: int = 0
