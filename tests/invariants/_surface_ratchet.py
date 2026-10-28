"""The owner public-surface ratchet the public-surface gates read.

Declared support surface (#476): the budget table lives here so the surface
detector and its arming premise import it from one declared home.
"""

from __future__ import annotations

__all__ = ["OWNER_PUBLIC_SURFACE_RATCHET"]

# Values are (declared public symbols, executable entry points).
OWNER_PUBLIC_SURFACE_RATCHET = {
    "app/admission/admission.py": (9, 9),
    "app/admission/admission_links.py": (11, 5),
    # RAISED to (14, 11) then LOWERED to (7, 6), (5, 5), (4, 4), then (2, 2): HostPolicy and
    # registrable_domain
    "app/safety/validation.py": (2, 2),
    "app/config/env.py": (7, 5),
    # RAISED to (7, 6): failed_unified_result is the single failure projection.
    "app/api/response_shaping.py": (7, 6),
    "app/api/unified_shaping.py": (5, 5),
    "app/storage/sqlite_store.py": (9, 7),
    "app/safety/safe_regex.py": (7, 2),
    # robots_pattern_to_regex is public (#190) -- robots_allows itself calls it -- so the
    # parity/per-host tests drive that real contract; the matcher class behind it stays private.
    "app/transport/robots.py": (13, 4),
    "app/safety/bounded_io.py": (4, 2),
    "app/transport/decompression.py": (5, 2),
    "app/admission/credentials.py": (3, 3),
    "app/admission/render_options.py": (2, 2),
    # RAISED to (2, 2): `payload_policy` was `_payload_policy` until the layout put its only production caller
    # (the engine) in another package.
    "app/admission/payload_helpers.py": (2, 2),
    # RAISED to (10, 9): terminal_attempt became public when CrawlResult stopped copying four axes out of the
    # attempt it holds.
    "app/transport/classification.py": (3, 2),
    # Designed RAISE: engine and render result funnels were merged per #324; LOWERED
    # to (8, 7) when the pure constructors moved to app/models/result_factory.py (#160).
    "app/transport/results.py": (8, 7),
    "app/transport/fetch_policy.py": (5, 5),
    "app/models/host_policy.py": (6, 5),
    # The two latest surface modules: the gate must not silently drop a module
    # added since the list was pinned (these prune handlers/helpers via __all__).
    "app/api/keys_router.py": (4, 3),
    "app/api/crawl_endpoint.py": (4, 4),
    # #127: the pool concept splits on a real async/sync axis -- AsyncPool for
    # BrowserPool, SyncPool for DocExtractionPool -- so the surface is two protocols.
    "app/runtime/pool.py": (2, 2),
    # CookieJar transport module with equivalence pin for contributor derivation (#418).
    "app/transport/cookie_jar.py": (13, 8),
    # #441 + #544/S3: the header wire law for names and values, shared by the
    # policy, credential, and transport edges.
    "app/safety/header_values.py": (3, 3),
    # Four transport/spill surfaces pinned with their true public doors; growing
    # any of these needs an explicit ratchet edit, shrinking auto-tightens.
    "app/transport/scheduler_purge.py": (2, 2),
    "app/orchestration/spill_sink.py": (2, 2),
    "app/transport/http_cache.py": (4, 4),
    "app/transport/http_classify.py": (3, 3),
    "app/transport/pooled_clients.py": (11, 10),
}
