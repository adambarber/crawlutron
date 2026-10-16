from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Credential(BaseModel):
    """A scoped HTTP credential, keyed by canonical origin (scheme://host[:port]).

    The per-hop redirect scrub then enforces "credentials never cross host boundaries"
    for free; values are secrets, never logged or echoed in diagnostics.
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
