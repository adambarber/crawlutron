"""The scoped HTTP credential record."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationInfo, field_validator, model_validator
from safe_url.url_redaction import redacted_marker

from crawlutron.safety.header_values import reject_injectable_header_value


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
        SecretStr | None,
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
        at the read edge.
        """
        if value is not None and info.field_name is not None:
            secret = value.get_secret_value() if isinstance(value, SecretStr) else value
            if redacted_marker() in secret:
                raise ValueError(
                    f"{info.field_name}: redacted marker is not a live credential value"
                    " -- re-supply the caller's credential to resume"
                )
            reject_injectable_header_value(info.field_name, secret)
        return value

    @model_validator(mode="after")
    def _match_kind(self) -> Credential:
        if self.kind == "bearer":
            if not (self.token and self.token.get_secret_value()):
                raise ValueError("bearer credential requires a token")
            if self.username is not None or self.password is not None:
                raise ValueError("bearer credential must not carry basic fields (username/password)")
        if self.kind == "basic":
            if not (self.username and self.password and self.password.get_secret_value()):
                raise ValueError("basic credential requires username and password")
            if self.token is not None:
                raise ValueError("basic credential must not carry a bearer token")
        return self
