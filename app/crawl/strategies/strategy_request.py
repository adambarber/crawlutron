"""Owns the per-page identity every strategy is handed.

This module owns nothing but the data: it must not fetch, decode, extract, or
construct crawl results.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.api_models import CrawlPolicy
from app.models.taxonomy import CrawlStrategyName
from app.validation import HostPolicy


@dataclass(frozen=True)
class StrategyRequest:
    """What a strategy is being asked to fetch, in one piece.

    These five -- original URL, canonical URL, strategy name, policy mapping,
    host policy -- were spelled as keyword parameters at both strategy entry
    points and then again as constructor arguments when the HTTP path built its
    context, so the identity of one page had three independent spellings. That
    is more than tidiness: mixing two pages' URLs is a correctness bug that
    reads as a plausible-looking result rather than an exception, and a parameter
    list with five same-shaped members is exactly the shape a transposition hides
    in. ``frozen`` because a strategy must not be able to rewrite the page it
    was dispatched for; the context objects downstream copy what they need.
    """

    original_url: str
    normalized_url: str
    strategy: CrawlStrategyName
    policy: CrawlPolicy
    host_policy: HostPolicy


__all__ = ["StrategyRequest"]
