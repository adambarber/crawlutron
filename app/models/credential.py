"""A scoped HTTP credential, keyed by canonical origin (scheme://host[:port]).

The per-hop redirect scrub then enforces "credentials never cross host boundaries"
for free; values are secrets, never logged or echoed in diagnostics.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationInfo, field_validator, model_validator

from app.safety.header_values import reject_injectable_header_value


class Credential(BaseModel):
    """A scoped HTTP credential, keyed by canonical origin (scheme://host[:port]).

    The per-hop redirect scrub then enforces "credentials never cross host boundaries"
    for free; values are secrets, never logged or echoed in diagnostics.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["bearer", "basic"]
    token: Annotated[
        SecretStr | None,
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
        SecretStr | None,
        Field(
            min_length=1,
            max_length=512,
            description="Basic password (kind=basic).",
        ),
    ] = None

    @field_validator("token", "username", "password")
    @classmethod
    def _reject_injectable_secret_values(
        cls, value: SecretStr | str | None, info: ValidationInfo
    ) -> SecretStr | str | None:
        """A credential is a header VALUE at one remove: a bearer token rides
        Authorization verbatim, so the wire law applies to every secret field,
        at the read edge (#441).
        """
        if value is not None and info.field_name is not None:
            secret = value.get_secret_value() if isinstance(value, SecretStr) else value
            reject_injectable_header_value(info.field_name, secret)
        return value

    @model_validator(mode="after")
    def _match_kind(self) -> Credential:
        if self.kind == "bearer" and not (self.token and self.token.get_secret_value()):
            raise ValueError("bearer credential requires a token")
        if self.kind == "basic" and not (
            self.username and self.password and self.password.get_secret_value()
        ):
            raise ValueError("basic credential requires username and password")
        return self
