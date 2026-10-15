from __future__ import annotations

from typing import TypedDict

from pydantic import BaseModel


class DiscoveryPayload(TypedDict):
    """The well-known discovery record, before pydantic validation.

    Names the shape the raw dict must satisfy instead of hiding a dropped
    key behind ``Any``.
    """

    service: str
    version: str
    policy_schema_version: str
    links: dict[str, str]
    recommended_endpoints: list[str]
    description: str


class DiscoveryResponse(BaseModel):
    service: str
    version: str
    policy_schema_version: str
    links: dict[str, str]
    recommended_endpoints: list[str]
    description: str
