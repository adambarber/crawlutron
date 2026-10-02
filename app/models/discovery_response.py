from __future__ import annotations

from pydantic import BaseModel


class DiscoveryResponse(BaseModel):
    service: str
    version: str
    policy_schema_version: str
    links: dict[str, str]
    recommended_endpoints: list[str]
    description: str
