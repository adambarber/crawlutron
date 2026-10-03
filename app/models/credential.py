from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Credential(BaseModel):
    """A scoped HTTP credential, scoped to one origin.

    Keyed in ``CrawlPolicy.credentials`` by canonical origin
    (``scheme://host[:port]``), so a credential is addressable only for the
    origin it was written for -- the per-hop redirect scrub for
    ``authorization`` then enforces "credentials never cross host
    boundaries" for free. Values are secrets: never logged, never echoed in
    diagnostics (policy echoes strip them, same discipline as cookies).
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["bearer", "basic"]
    token: Annotated[
        str | None,
        Field(
            min_length=1,
            max_length=512,
            description="Bearer token (kind=bearer).",
        ),
    ] = None
    username: Annotated[
        str | None,
        Field(
            min_length=1,
            max_length=256,
            description="Basic username (kind=basic).",
        ),
    ] = None
    password: Annotated[
        str | None,
        Field(
            min_length=1,
            max_length=512,
            description="Basic password (kind=basic).",
        ),
    ] = None

    @model_validator(mode="after")
    def _match_kind(self) -> Credential:
        if self.kind == "bearer" and not self.token:
            raise ValueError("bearer credential requires a token")
        if self.kind == "basic" and not (self.username and self.password):
            raise ValueError("basic credential requires username and password")
        return self
