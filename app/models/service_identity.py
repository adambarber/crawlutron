from __future__ import annotations

from typing import Final

from app.constants import SERVICE_ID, SERVICE_VERSION
from app.models.crawl_policy import POLICY_SCHEMA_VERSION

#: The identity block every discovery response opens with. Two payloads were
#: spelling the same three keys, and a top-level key reaching one endpoint and not
#: the other is exactly the shape of bug this codebase shipped once -- a strict
#: ``response_model`` DROPS it without complaint, so a raw-dict unit test stays
#: green while the wire loses the field. One block means a fourth key is added in
#: one place and cannot reach only some of the endpoints.
SERVICE_IDENTITY: Final[dict[str, str]] = {
    "service": SERVICE_ID,
    "version": SERVICE_VERSION,
    "policy_schema_version": POLICY_SCHEMA_VERSION,
}
