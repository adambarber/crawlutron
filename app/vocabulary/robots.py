"""The vocabulary of how one origin's robots policy was obtained.

The rules alone cannot say why a crawl was refused: a forbidding policy, a
refused one and an unreachable one all deny, and the caller acts differently.
"""

from __future__ import annotations

from enum import StrEnum

from app.models.taxonomy import CrawlError


class RobotsSource(StrEnum):
    """How one origin's ``robots.txt`` answer was obtained.

    ``policy`` is a policy that was read. ``refused`` is an application-layer
    refusal (401/403/429); ``unavailable`` is the other 4xx; ``unreachable`` is 5xx.
    """

    policy = "policy"
    refused = "refused"
    unavailable = "unavailable"
    unreachable = "unreachable"

    @property
    def error_code(self) -> CrawlError:
        """The error code a denial of a policy obtained this way carries.

        A property of the source, so the mapping lives with the vocabulary it
        describes and no other module has to keep a parallel table.
        """
        if self is RobotsSource.refused:
            return "robots_txt_refused"
        if self is RobotsSource.unreachable:
            return "robots_txt_unreachable"
        return "robots_txt_disallowed"
