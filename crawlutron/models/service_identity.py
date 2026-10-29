from __future__ import annotations

from typing import Final

from crawlutron.models.crawl_policy import POLICY_SCHEMA_VERSION
from crawlutron.vocabulary.identity import SERVICE_ID, SERVICE_VERSION

#: One identity block for every discovery response: a top-level key reaching one
#: endpoint and not the other is dropped silently, so a raw-dict test stays green.
SERVICE_IDENTITY: Final[dict[str, str]] = {
    "service": SERVICE_ID,
    "version": SERVICE_VERSION,
    "policy_schema_version": POLICY_SCHEMA_VERSION,
}
