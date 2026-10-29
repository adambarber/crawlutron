from __future__ import annotations

from typing import TypedDict

from pydantic import BaseModel, Field


class DiscoveryPayload(TypedDict):
    """The raw well-known discovery record: what discovery_payload builds."""

    service: str
    version: str
    policy_schema_version: str
    links: dict[str, str]
    recommended_endpoints: list[str]
    description: str
    # auth: caller-identity story; is_operator_sealed: the sealed keys-door flag.
    auth: dict[str, str]
    is_operator_sealed: bool


class DiscoveryResponse(BaseModel):
    """The validated wire record."""

    service: str
    version: str
    policy_schema_version: str
    links: dict[str, str]
    recommended_endpoints: list[str]
    description: str
    auth: dict[str, str] = Field(
        description="The caller-identity story: credential, anonymous caller, key management.",
    )
    is_operator_sealed: bool = Field(
        description="Whether the operator token is unset (the keys door refuses everything).",
        examples=[False],
    )


#: The wire contract the two arms above must agree on; DiscoveryPayload is the
#: declaration of record and this tuple pins its key order for the parity check.
DISCOVERY_WIRE_FIELDS = tuple(DiscoveryPayload.__annotations__)
