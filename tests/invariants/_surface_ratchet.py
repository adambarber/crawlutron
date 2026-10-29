"""The public-surface ratchets the public-surface gates read.

Declared support surface (#476): the budget tables live here so the surface
detector and its arming premise import them from one declared home.
"""

from __future__ import annotations

__all__ = ["OWNER_PUBLIC_SURFACE_RATCHET", "DECLARED_SURFACE_RATCHET"]

# Values are (declared public symbols, executable entry points).
OWNER_PUBLIC_SURFACE_RATCHET = {
    "app/admission/admission.py": (9, 9),
    "app/admission/admission_links.py": (12, 5),
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
    "app/admission/render_options.py": (3, 2),
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
    # LOWERED to (5, 5): HOST_POLICY_ENTRY_POINTS moved into the test helper.
    "app/models/host_policy.py": (5, 5),
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
    "app/transport/pooled_clients.py": (13, 11),
}

# Former #215 exemptions: their __all__ deliberately does not enumerate every public
# symbol, so these are counts-only rows pinned at measured counts; only-down from here.
DECLARED_SURFACE_RATCHET = {
    "app/admission/policy.py": (10, 5),
    "app/api/_depends_singletons.py": (2, 0),
    "app/api/async_crawl_runner.py": (4, 3),
    "app/api/caller_auth.py": (17, 14),
    "app/api/caller_concurrency.py": (1, 1),
    "app/api/crawl_executor.py": (1, 1),
    "app/api/crawl_router.py": (4, 3),
    "app/api/dependencies.py": (16, 7),
    "app/api/exceptions.py": (10, 7),
    "app/api/identity_router.py": (5, 3),
    "app/api/job_runner.py": (16, 13),
    "app/api/jobs_router.py": (6, 5),
    "app/api/main_helpers.py": (3, 3),
    "app/api/map_endpoint.py": (2, 1),
    "app/api/metrics_router.py": (2, 1),
    "app/api/middleware.py": (3, 2),
    "app/api/policy_defaults.py": (2, 0),
    "app/api/policy_router.py": (2, 1),
    "app/api/policy_suggestions.py": (3, 2),
    "app/api/render.py": (3, 3),
    "app/api/render_endpoint.py": (3, 2),
    "app/api/watch_sweep.py": (4, 2),
    "app/api/webhooks.py": (17, 16),
    "app/extraction/content.py": (4, 4),
    "app/extraction/llm_format.py": (4, 4),
    "app/mcp/constants.py": (18, 0),
    "app/mcp/protocol.py": (6, 6),
    "app/mcp/router.py": (2, 1),
    "app/mcp/server.py": (6, 5),
    "app/mcp/tools.py": (13, 8),
    "app/orchestration/admission_stall.py": (5, 3),
    "app/orchestration/recursive.py": (11, 7),
    "app/orchestration/request_pipeline.py": (5, 3),
    "app/transport/robots_parse.py": (6, 6),
    "app/orchestration/strategy_chain.py": (4, 2),
    "app/runtime/worker_dispatch.py": (1, 1),
    "app/runtime/logging_config.py": (1, 1),
    "app/runtime/pressure_sizing.py": (12, 8),
    "app/search/endpoint.py": (2, 1),
    "app/search/providers.py": (21, 9),
    "app/search/providers_api.py": (5, 5),
    "app/search/providers_html.py": (1, 1),
    "app/storage/coordination_cookies.py": (2, 1),
    "app/storage/coordination_counters.py": (1, 1),
    "app/storage/coordination_store.py": (4, 2),
    "app/storage/job_store.py": (2, 1),
    "app/storage/job_store_collections.py": (11, 6),
    "app/storage/job_store_read.py": (3, 2),
    "app/storage/job_store_state.py": (3, 2),
    "app/runtime/resource_lifecycle.py": (1, 1),
    "app/storage/migrations/runner.py": (5, 4),
    "app/storage/migrations/ddl.py": (2, 2),
    "app/storage/watch_store.py": (1, 1),
    "app/transport/browser.py": (3, 2),
    "app/transport/browser_cookies.py": (8, 5),
    "app/transport/browser_result.py": (1, 1),
    "app/transport/content_pipeline.py": (6, 4),
    "app/transport/http_client_pool.py": (2, 2),
    "app/transport/strategy_request.py": (1, 0),
}
