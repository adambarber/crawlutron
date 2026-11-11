"""HTTP door for ``/v1/map``: the mapping core moved to crawlutron/service (#849)."""

from crawlutron.service.map_endpoint import map_site

__all__ = ["map_site"]
