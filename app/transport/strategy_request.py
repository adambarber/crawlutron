"""Owns the per-page identity every strategy is handed.

This module owns nothing but the data: it must not fetch, decode, extract, or
construct crawl results.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models.crawl_policy import CrawlPolicy
from app.models.host_policy import HostPolicy
from app.models.normalized_url import NormalizedUrl
from app.models.taxonomy import CrawlStrategyName


@dataclass(frozen=True)
class StrategyRequest:
    """What a strategy is being asked to fetch, in one piece (five fields, once spelled).

    The identity of one page had three independent spellings; mixing two pages' URLs is a
    correctness bug that reads as plausible. ``frozen`` -- a strategy cannot rewrite its page.
    """

    original_url: str
    normalized_url: NormalizedUrl
    strategy: CrawlStrategyName
    policy: CrawlPolicy
    host_policy: HostPolicy


__all__ = ["StrategyRequest"]
