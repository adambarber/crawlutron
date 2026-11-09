"""The recommended_endpoints list, derived from the declared surface.

The hand-curated list this module replaces could drift both ways: an entry for
a dead route (the historical staleness tests caught that) and a live route no
agent ever finds (nothing caught that). The list is now COMPUTED: registry
routes plus the discovery doors, in the stable advertisement order, with the
operator trust plane excluded.
"""

from __future__ import annotations

from crawlutron.discovery.recommended_surface import advertised_route_strings

__all__ = ["recommended_endpoints"]


def recommended_endpoints() -> list[str]:
    """Every caller-facing surface, computed from the registry and discovery doors."""
    return advertised_route_strings()
