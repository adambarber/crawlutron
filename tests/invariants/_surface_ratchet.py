"""The public-surface ratchets the public-surface gates read.

Declared support surface (#476): the budget tables live here so the surface
detector and its arming premise import them from one declared home.
"""

from __future__ import annotations

__all__ = ["OWNER_PUBLIC_SURFACE_RATCHET", "DECLARED_SURFACE_RATCHET"]

# Values are (declared public symbols, executable entry points).
OWNER_PUBLIC_SURFACE_RATCHET = {
    "crawlutron/admission/admission.py": (9, 9),
    # gather_deadline_bounded is public (#323): render admission reuses the crawl-path gather.
    "crawlutron/admission/admission_links.py": (13, 6),
    # RAISED to (14, 11) then LOWERED to (7, 6), (5, 5), (4, 4), then (2, 2): HostPolicy and
    # registrable_domain
    "crawlutron/safety/validation.py": (2, 2),
    # RAISED to (8, 6) by #621: EnvDecisionMemo is the one shared env-decision memo
    # (it replaced two hand-copied lumps; net surface across the memo modules went DOWN).
    "crawlutron/config/env.py": (8, 6),
    # RAISED to (7, 6): failed_unified_result is the single failure projection.
    "crawlutron/service/response_shaping.py": (7, 6),
    "crawlutron/service/unified_shaping.py": (5, 5),
    # #861: the render core moved from api/ to service/; logger is private so
    # surface is two public symbols, both entry points.
    "crawlutron/service/render.py": (3, 3),
    "crawlutron/service/render_endpoint.py": (2, 2),
    # Delta crawl classification (#714); RAISED to (14, 7) for BaselineEntry and
    # extract_baseline_entries/removed_entry_keys, the per-entry removal substrate.
    "crawlutron/delta.py": (14, 7),
    # RAISED to (10, 8) by #314/F2: connection_data_version is the one PRAGMA read
    # chokepoint outside configure_store_connection, backing the table-trust digest.
    "crawlutron/storage/sqlite_store.py": (11, 9),
    "crawlutron/safety/safe_regex.py": (7, 2),
    # robots_pattern_to_regex is public (#190) -- robots_allows itself calls it; the matcher class
    # behind it stays private, and RobotsMatcher stays public as the function's return type.
    "crawlutron/transport/robots.py": (13, 4),
    "crawlutron/safety/bounded_io.py": (4, 2),
    "crawlutron/transport/decompression.py": (5, 2),
    "crawlutron/admission/credentials.py": (3, 3),
    "crawlutron/admission/render_options.py": (3, 2),
    # RAISED to (2, 2): `payload_policy` was `_payload_policy` until the layout put its only production caller
    # (the engine) in another package.
    "crawlutron/admission/payload_helpers.py": (2, 2),
    # LOWERED to (3, 2): terminal_attempt moved to crawlutron/models/result_factory.py (#160).
    "crawlutron/transport/classification.py": (3, 2),
    # Designed RAISE: engine and render result funnels were merged per #324; LOWERED
    # to (8, 7) when the pure constructors moved to crawlutron/models/result_factory.py (#160).
    "crawlutron/transport/results.py": (8, 7),
    "crawlutron/transport/fetch_policy.py": (5, 5),
    # LOWERED to (5, 5): HOST_POLICY_ENTRY_POINTS moved into the test helper.
    "crawlutron/models/host_policy.py": (5, 5),
    # The two latest surface modules: the gate must not silently drop a module
    # added since the list was pinned (these prune handlers/helpers via __all__).
    "crawlutron/api/keys_router.py": (4, 3),
    # #645: the two read cores joined. #714: delta_context_for is the shared
    # delta loader. #849: the cores moved to service.crawl_endpoint_cores.
    "crawlutron/api/crawl_endpoint.py": (4, 4),
    # #127: the pool concept splits on a real async/sync axis -- AsyncPool for
    # BrowserPool, SyncPool for DocExtractionPool -- so the surface is two protocols.
    "crawlutron/runtime/pool.py": (2, 2),
    # CookieJar transport module with equivalence pin for contributor derivation (#418).
    "crawlutron/transport/cookie_jar.py": (11, 6),
    # #441 + #544/S3: the header wire law for names and values, shared by the
    # policy, credential, and transport edges.
    "crawlutron/safety/header_values.py": (3, 3),
    # #314/A3 raised the state row to (2, 2) for DomainGate, whose public
    # occupancy answers liveness. Growing any of these needs an explicit edit.
    "crawlutron/transport/scheduler_domain_state.py": (2, 2),
    "crawlutron/transport/scheduler_pacing.py": (6, 4),
    "crawlutron/transport/http_cache.py": (4, 4),
    "crawlutron/transport/http_classify.py": (3, 3),
    "crawlutron/transport/pooled_clients.py": (9, 6),
    # #820/unit-3: release machinery split out of pooled_clients for the ceiling.
    "crawlutron/transport/pooled_client_release.py": (10, 8),
    "crawlutron/transport/pooled_socket_abort.py": (1, 1),
    # #314/wave-2: JobAccumulators (one class) moved here whole from job_store_state
    # to hold the 500-line ceiling; the seam owns every accumulator table.
    "crawlutron/storage/job_store_accumulators.py": (1, 1),
    # #314/wave-3: DNS + SSRF split out of pinned_transport (500-line ceiling);
    # the seam owns every resolve-and-pin door plus the reset pair. No test doors:
    "crawlutron/transport/pinned_dns.py": (11, 8),  # saturation via the real resolve door (ParkedResolver)
}

# Former #215 exemptions: their __all__ deliberately does not enumerate every public
# symbol, so these are counts-only rows pinned at measured counts; only-down from here.
DECLARED_SURFACE_RATCHET = {
    # #643: unregister joins the registry's public door set so test probes can
    # drop their fake owners (registered_owners exhaustiveness must stay true).
    "crawlutron/runtime/process_state.py": (6, 6),
    # Generated at build time by the hatch-vcs hook (#561); never hand-edited.
    "crawlutron/_version.py": (3, 0),
    "crawlutron/admission/policy.py": (10, 5),
    "crawlutron/api/_depends_singletons.py": (2, 0),
    "crawlutron/service/async_crawl_runner.py": (3, 2),
    # #664 raised 18,15 -> 19,16 (verify_admin_secret); #822 raised 21,18 -> 22,19
    # (operator_caller: the shared operator identity the keys door and resolver read).
    "crawlutron/service/cache_sweep.py": (3, 2),
    "crawlutron/service/sweep_loop.py": (2, 1),
    # Spill sweep (#753): one GC lane beside the cache sweep; max_age is wired
    # from the result store's ttl_seconds, not a second literal.
    "crawlutron/service/spill_sweep.py": (4, 2),
    "crawlutron/service/store_shutdown.py": (1, 1),
    # #849: the core moved to service/caller_auth; Request plumbing stays api.
    "crawlutron/api/caller_auth.py": (11, 9),
    "crawlutron/service/caller_concurrency.py": (1, 1),
    # #674 raised 1,1 -> 2,2: CrawlRefusalError is the typed refusal carrier the
    # sweep's deactivation arm reads (reason as an enum member, not boundary text).
    "crawlutron/service/crawl_executor.py": (2, 2),
    "crawlutron/api/crawl_router.py": (4, 3),
    # #656 raised 16,7 -> 18,8: get_concurrency_tracker + TrackerDep make the
    # app-state stash load-bearing (the getattr-None scatter is refused loudly).
    "crawlutron/api/dependencies.py": (22, 10),
    # #813: is_quota_refusal is the named quota-refusal match the sweep reads.
    # #849: the predicate single-homed to service/errors; handler joins here.
    "crawlutron/api/exceptions.py": (10, 8),
    # #668: the single-homed discovery-link constants the 404 details reuse.
    "crawlutron/discovery/links.py": (3, 0),
    # #649: derived discovery -- one thin re-export and one surface walk.
    "crawlutron/discovery/recommended.py": (1, 1),
    "crawlutron/discovery/recommended_surface.py": (3, 1),
    "crawlutron/api/router_identity.py": (1, 1),
    "crawlutron/api/identity_router.py": (5, 3),
    "crawlutron/service/job_runner.py": (15, 12),
    "crawlutron/api/jobs_router.py": (6, 5),
    "crawlutron/service/main_helpers.py": (3, 3),
    "crawlutron/service/map_endpoint.py": (2, 1),
    "crawlutron/api/metrics_router.py": (2, 1),
    "crawlutron/api/middleware.py": (4, 2),
    # #653: the key-verify meter (one class + the shared instance accessor).
    "crawlutron/api/admit_router.py": (2, 1),
    # #677: known_reasons is the public door the producer->table loop gate reads.
    "crawlutron/service/admit_endpoint.py": (3, 2),
    "crawlutron/service/key_throttle.py": (2, 2),
    "crawlutron/service/policy_defaults.py": (2, 0),
    "crawlutron/api/policy_router.py": (2, 1),
    "crawlutron/api/mcp_router.py": (2, 1),
    # RAISED to (14, 12) by #854: the CallerQuotaAdmission port (Protocol, concrete
    # binding, default instance) joins the core so doors consume the interface.
    "crawlutron/service/caller_auth.py": (14, 12),
    "crawlutron/service/crawl_endpoint_cores.py": (4, 4),
    "crawlutron/service/policy_suggestions.py": (3, 2),
    "crawlutron/service/watch_sweep.py": (4, 2),
    # #849: the typed domain refusals both doors map — one base + named subclasses
    # plus the single-homed quota predicate the sweep reads.
    "crawlutron/service/errors.py": (17, 16),
    "crawlutron/service/webhooks.py": (15, 13),
    "crawlutron/extraction/content.py": (4, 4),
    "crawlutron/extraction/llm_format.py": (4, 4),
    # #668 raised 18,0 -> 19,0: RATE_LIMITED is the JSON-RPC code the anonymous
    # meter's /mcp refusal answers with.
    "crawlutron/mcp/constants.py": (19, 0),
    "crawlutron/mcp/protocol.py": (6, 6),
    # #849: the MCP HTTP router relocated to api/mcp_router (wire concern); the
    # protocol layer imports service only.
    "crawlutron/mcp/server.py": (6, 5),
    # #714: the MCP crawl doors' projection split (projection enum + projected);
    # handlers.py back under the 500-line ceiling.
    "crawlutron/mcp/crawl_shaping.py": (2, 1),
    # #645: tools.py split into registry + handlers (500-line ceiling); the read
    # tools joined. Closures lowered both entry counts.
    "crawlutron/mcp/registry.py": (4, 1),
    # #646: the four job-tool handlers joined via _job_tool_handler. #647: create_job.
    # ARCH-1: factory collapse (data-driven dispatch) deleted the factories + watch_handlers.py.
    "crawlutron/mcp/handlers.py": (9, 6),  # RAISED to (9, 6) by #701: DoorEnv is the typed door environment.
    # #646: the one control-plane transition both job doors run, plus its shared 404/409 raisers.
    "crawlutron/service/job_control.py": (3, 3),
    # #647: the typed job cores both doors run (create/list/get/control/delete).
    "crawlutron/service/job_cores.py": (5, 5),
    # #648: the typed watch cores both doors run (register/list/get/delete).
    "crawlutron/service/watch_cores.py": (4, 4),
    # #644: the one declaration of the module-level feature routers.
    "crawlutron/api/router_registry.py": (1, 0),
    # #664: the auth+metering layer (guards, router factories, ratchet row).
    # #668: PENDING sealed at zero and left; ANONYMOUS_METERED joined (12, 5).
    "crawlutron/api/route_layer.py": (12, 5),
    # #818: the fail-closed boot validator for the layer-exempt ratchet.
    "crawlutron/api/exempt_route_boot.py": (2, 2),
    # #668: the anonymous IP-bucket meter over the docs surfaces, /mcp
    # anonymous traffic, and the 404 route-miss (metered on the response stage).
    "crawlutron/api/anonymous_throttle.py": (10, 4),
    # #668: the shared process-meter skeleton the two throttles compose (the
    # jscpd logic gate's extraction, replacing two hand-copied ledger triples).
    "crawlutron/service/meter_ledger.py": (2, 2),
    "crawlutron/service/settings_access.py": (2, 1),
    "crawlutron/orchestration/admission_stall.py": (6, 4),
    "crawlutron/orchestration/recursive.py": (11, 7),
    "crawlutron/orchestration/request_pipeline.py": (5, 3),
    "crawlutron/transport/robots_parse.py": (6, 6),
    "crawlutron/orchestration/strategy_chain.py": (4, 2),
    "crawlutron/runtime/worker_dispatch.py": (1, 1),
    "crawlutron/runtime/logging_config.py": (1, 1),
    "crawlutron/runtime/pressure_sizing.py": (12, 8),
    "crawlutron/search/endpoint.py": (2, 1),
    "crawlutron/search/providers.py": (21, 9),
    "crawlutron/search/providers_api.py": (5, 5),
    "crawlutron/search/providers_html.py": (1, 1),
    "crawlutron/storage/admin_audit_store.py": (4, 1),
    "crawlutron/storage/coordination_cookies.py": (2, 1),
    "crawlutron/storage/coordination_counters.py": (1, 1),
    "crawlutron/storage/coordination_store.py": (4, 2),
    "crawlutron/storage/job_store.py": (2, 1),
    "crawlutron/storage/job_store_collections.py": (11, 6),
    "crawlutron/storage/job_store_read.py": (3, 2),
    "crawlutron/storage/job_store_state.py": (2, 1),
    "crawlutron/runtime/resource_lifecycle.py": (8, 8),  # (8,8) #681 mapping record; (5,5) #643 decline
    "crawlutron/storage/migrations/runner.py": (5, 4),
    "crawlutron/storage/migrations/ddl.py": (2, 2),
    "crawlutron/storage/watch_store.py": (1, 1),
    "crawlutron/transport/browser.py": (3, 2),
    "crawlutron/transport/browser_cookies.py": (6, 3),
    "crawlutron/transport/browser_result.py": (1, 1),
    "crawlutron/transport/content_pipeline.py": (6, 4),
    "crawlutron/transport/http_client_pool.py": (1, 1),
    "crawlutron/transport/strategy_request.py": (1, 0),
    # Issue #710: crawl CLI facade. Counts derived by running the gate, not
    # predicted.
    "crawlutron/cli/__main__.py": (11, 2),
    "crawlutron/cli/client.py": (3, 1),
    "crawlutron/cli/docker.py": (11, 1),
    "crawlutron/cli/output.py": (1, 1),
}
