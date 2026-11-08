from __future__ import annotations

from typing import TypedDict

from pydantic import BaseModel


class DiscoveryPayload(TypedDict):
    """The raw well-known discovery record: what discovery_payload builds.

    The declaration of record: the validated twin below redeclares these fields
    and the surface-contract invariant proves the two field sets agree.
    """

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
    """The validated wire record.

    ``auth`` is the caller-identity story: credential, anonymous caller, key
    management. ``is_operator_sealed``: whether the operator token is unset
    (the keys door refuses everything).
    """

    service: str
    version: str
    policy_schema_version: str
    links: dict[str, str]
    recommended_endpoints: list[str]
    description: str
    auth: dict[str, str]
    is_operator_sealed: bool


#: The wire contract the two arms above must agree on; DiscoveryPayload is the
#: declaration of record and this tuple pins its key order for the parity check.
DISCOVERY_WIRE_FIELDS = tuple(DiscoveryPayload.__annotations__)
