"""The recommended_endpoints list, derived from the declared surface.

The hand-curated list this module replaces could drift both ways (a dead-route
entry, an unadvertised live route). The list is now COMPUTED from the registry.
"""

from __future__ import annotations

from crawlutron.discovery.recommended_surface import advertised_route_strings

__all__ = ["recommended_endpoints"]


def recommended_endpoints() -> list[str]:
    """Every caller-facing surface, computed from the registry and discovery doors."""
    return advertised_route_strings()
