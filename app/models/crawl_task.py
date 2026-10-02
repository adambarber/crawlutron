"""A single URL scheduled for crawling within a crawl request."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CrawlTask:
    """Carries the per-job bookkeeping the engine/recursive dispatcher needs.

    Submission index for result ordering, the original URL, the durable page
    identity, and the registrable domain used for per-domain scheduling and
    robots scoping.

    ``normalized_url`` is ``str``, not ``NormalizedUrl``, because it is the
    durable page identity: ``durable_page_identity`` answers with the raw value
    when a URL has no canonical form, and a field that claims admission it
    cannot prove is how a raw seed became indistinguishable from a normalized
    one. Admission is proved at the boundary that enforces it --
    ``AdmittedLink.normalized_url``.
    """

    index: int
    original_url: str
    normalized_url: str
    domain: str
