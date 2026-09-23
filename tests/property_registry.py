from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Suppression:
    module: str
    symbol: str
    fingerprint: str
    reason: str


@dataclass(frozen=True)
class Property:
    name: str
    owner_module: str
    owner_symbols: tuple[str, ...]
    definition_site: str
    fingerprints: tuple[str, ...]
    reason: str
    suppressions: tuple[Suppression, ...] = ()


PROPERTY_REGISTRY = (
    Property(
        name="public-target",
        owner_module="app/validation.py",
        owner_symbols=("_host_is_public", "_is_public_ip", "_is_public_ip_value", "_classified_ip_address"),
        definition_site="app/validation.py:_is_public_ip",
        fingerprints=("ipaddress_range_verdict",),
        reason="URL normalization and HTTP pinning must share one public-address verdict.",
    ),
    Property(
        name="bool-env",
        owner_module="app/validation.py",
        owner_symbols=("parse_bool_value",),
        definition_site="app/validation.py:parse_bool_value",
        fingerprints=("bool_literal_set",),
        reason="Environment boolean spelling is one policy across settings and feature gates.",
    ),
    Property(
        name="robots-policy",
        owner_module="app/crawl/robots.py",
        owner_symbols=("_load_uncached", "load_robots_rules", "robots_allows"),
        definition_site="app/crawl/robots.py:load_robots_rules",
        fingerprints=(
            "robots_url",
            "parse_robots_rules_call",
            "parse_robots_rules_definition",
            "robots_bounded_ttl_cache",
        ),
        reason="The engine and durable admission path must share one robots policy loader and cache.",
        suppressions=(
            Suppression(
                module="app/crawl/robots_parse.py",
                symbol="parse_robots_rules",
                fingerprint="parse_robots_rules_definition",
                reason=(
                    "The line-oriented robots parser is a shared lower-level primitive; the policy owner "
                    "is the only module that fetches, caches, and serves its result."
                ),
            ),
            Suppression(
                module="app/crawl/robots_parse.py",
                symbol="parse_robots_crawl_delay",
                fingerprint="parse_robots_rules_definition",
                reason=(
                    "Crawl-delay parsing is part of the shared line-oriented parser primitive consumed "
                    "by the policy owner."
                ),
            ),
            Suppression(
                module="app/crawl/robots_parse.py",
                symbol="parse_robots_sitemaps",
                fingerprint="parse_robots_rules_definition",
                reason=(
                    "Sitemap extraction is part of the shared line-oriented parser primitive consumed "
                    "by the policy owner."
                ),
            ),
            Suppression(
                module="app/crawl/strategies/http.py",
                symbol="_discovery_diagnostics",
                fingerprint="robots_url",
                reason=(
                    "This legacy diagnostics-only fallback reports discovery metadata when the engine "
                    "has no cached sitemap hints; it is not a policy decision or cache owner."
                ),
            ),
        ),
    ),
)

PROPERTY_BY_NAME = {entry.name: entry for entry in PROPERTY_REGISTRY}
