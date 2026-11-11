"""The discovery links the service advertises, single-homed (#668).

The 404 details, the well-known payload, and the capabilities payload all
name these doors; one module owns the strings (the drift law).
"""

from __future__ import annotations

__all__ = ["DISCOVERY_LINKS", "OPENAPI_DOCS_LINK", "WELL_KNOWN_LINK"]

OPENAPI_DOCS_LINK = "/docs"
WELL_KNOWN_LINK = "/.well-known/crawler-service"

#: The pair the 404 details carry: the machine door and the human docs, both
#: already advertised by discovery/capabilities.py and discovery/well_known.py.
DISCOVERY_LINKS = {
    "capabilities": WELL_KNOWN_LINK,
    "docs": OPENAPI_DOCS_LINK,
}
