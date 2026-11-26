"""The public-surface ratchets the public-surface gates read.

Declared support surface (#476): the budget tables live here so the surface
detector and its arming premise import them from one declared home.
"""

from __future__ import annotations

__all__ = ["OWNER_PUBLIC_SURFACE_RATCHET", "DECLARED_SURFACE_RATCHET"]

# Values are (declared public symbols, executable entry points).
OWNER_PUBLIC_SURFACE_RATCHET = {
    "packages/core/src/crawlutron/admission/admission.py": (9, 9),
    # gather_deadline_bounded is public (#323): render admission reuses the crawl-path gather.
    "packages/core/src/crawlutron/admission/admission_links.py": (13, 6),
    # RAISED to (14, 11) then LOWERED to (7, 6), (5, 5), (4, 4), then (2, 2): HostPolicy and
    # registrable_domain moved to safe_url (#998).
    "packages/core/src/crawlutron/safety/validation.py": (2, 2),
    # RAISED to (8, 6) by #621: EnvDecisionMemo is the one shared env-decision memo
    # (it replaced two hand-copied lumps; net surface across the memo modules went DOWN).
    "packages/core/src/crawlutron/config/env.py": (8, 6),
    # RAISED to (7, 6): failed_unified_result is the single failure projection.
    "packages/core/src/crawlutron/service/response_shaping.py": (7, 6),
    "packages/core/src/crawlutron/service/unified_shaping.py": (7, 7),
    # #861: the render core moved from api/ to service/; logger is private so
    # surface is two public symbols, both entry points.
    "packages/core/src/crawlutron/service/render.py": (3, 3),
    "packages/core/src/crawlutron/service/render_endpoint.py": (2, 2),
    # Delta crawl classification (#714); RAISED to (14, 7) for BaselineEntry and
    # extract_baseline_entries/removed_entry_keys, the per-entry removal substrate.
    "packages/core/src/crawlutron/delta.py": (14, 7),
    # RAISED to (10, 8) by #314/F2: connection_data_version is the one PRAGMA read
    # chokepoint outside configure_store_connection, backing the table-trust digest.
    "packages/core/src/crawlutron/storage/sqlite_store.py": (11, 9),
    # robots_pattern_to_regex is public (#190) -- robots_allows itself calls it; the matcher class
    # behind it stays private, and RobotsMatcher stays public as the function's return type.
    "packages/core/src/crawlutron/transport/robots.py": (13, 4),
    "packages/core/src/crawlutron/safety/bounded_io.py": (4, 2),
    "packages/core/src/crawlutron/transport/decompression.py": (5, 2),
    "packages/core/src/crawlutron/admission/credentials.py": (3, 3),
    # #1136: policy_wants_markdown is the markdown opt-in reader, mirroring
    # policy_wants_html; it raises the declared surface by one symbol.
    "packages/core/src/crawlutron/admission/render_options.py": (4, 3),
    # RAISED to (2, 2): `payload_policy` was `_payload_policy` until the layout put its only production caller
    # (the engine) in another package.
    "packages/core/src/crawlutron/admission/payload_helpers.py": (2, 2),
    # LOWERED to (3, 2): terminal_attempt moved to crawlutron/models/result_factory.py (#160).
    "packages/core/src/crawlutron/transport/classification.py": (3, 2),
    # Designed RAISE: engine and render result funnels were merged per #324; LOWERED
    # to (8, 7) when the pure constructors moved to crawlutron/models/result_factory.py (#160).
    "packages/core/src/crawlutron/transport/results.py": (8, 7),
    "packages/core/src/crawlutron/transport/fetch_policy.py": (5, 5),
    # #998: HostPolicy moved to safe_url/host_policy.py; this file is gone.
    # The next two surface modules are new since the list was pinned.
    "packages/rest/src/crawlutron_rest/api/keys_router.py": (5, 4),  # #823: rotate door entry point
    # #645: the two read cores joined. #714: delta_context_for is the shared
    # delta loader. #849: the cores moved to service.crawl_endpoint_cores.
    "packages/rest/src/crawlutron_rest/api/crawl_endpoint.py": (4, 4),
    # #127: the pool concept splits on a real async/sync axis -- AsyncPool for
    # BrowserPool, SyncPool for DocExtractionPool -- so the surface is two protocols.
    "packages/core/src/crawlutron/runtime/pool.py": (2, 2),
    # CookieJar transport module with equivalence pin for contributor derivation (#418).
    "packages/core/src/crawlutron/transport/cookie_jar.py": (11, 6),
    # #441 + #544/S3: the header wire law for names and values, shared by the
    # policy, credential, and transport edges.
    "packages/core/src/crawlutron/safety/header_values.py": (3, 3),
    # #314/A3 raised the state row to (2, 2) for DomainGate, whose public
    # occupancy answers liveness. Growing any of these needs an explicit edit.
    "packages/core/src/crawlutron/transport/scheduler_domain_state.py": (2, 2),
    "packages/core/src/crawlutron/transport/scheduler_pacing.py": (6, 4),
    "packages/core/src/crawlutron/transport/http_cache.py": (4, 4),
    "packages/core/src/crawlutron/transport/http_classify.py": (3, 3),
    "packages/core/src/crawlutron/transport/pooled_clients.py": (9, 6),
    # #820/unit-3: release machinery split out of pooled_clients for the ceiling.
    "packages/core/src/crawlutron/transport/pooled_client_release.py": (10, 8),
    "packages/core/src/crawlutron/transport/pooled_socket_abort.py": (1, 1),
    # #314/wave-2: JobAccumulators (one class) moved here whole from job_store_state
    # to hold the 500-line ceiling; the seam owns every accumulator table.
    "packages/core/src/crawlutron/storage/job_store_accumulators.py": (1, 1),
    # #314/wave-3: DNS + SSRF split out of pinned_transport (500-line ceiling);
    # the seam owns every resolve-and-pin door plus the reset pair. No test doors:
    "packages/core/src/crawlutron/transport/pinned_dns.py": (
        10,
        7,
    ),  # saturation via the real resolve door (ParkedResolver)
    # #1448: loopback proxy door + lifecycle surface.
    # #1456: typed sidecar_bind admission (IPv4Address) folded into serve_proxy.
    "packages/core/src/crawlutron/transport/egress_proxy.py": (5, 4),
    # #1452: the sync-builder seam that owns the proxy listener lifecycle.
    "packages/core/src/crawlutron/transport/egress_proxy_runtime.py": (3, 3),
    # #998: safe-url package modules.
    "packages/safe-url/src/safe_url/address_class.py": (3, 2),
    "packages/safe-url/src/safe_url/host_canonical.py": (2, 2),
    "packages/safe-url/src/safe_url/host_policy.py": (5, 5),
    "packages/safe-url/src/safe_url/normalized_url.py": (6, 4),
    "packages/safe-url/src/safe_url/process_state.py": (6, 6),
    # LOWERED to (2, 2): the PSL heuristic, TWO_PART_PUBLIC_SUFFIXES and
    # _psl_available were deleted -- hard deps now, no absent state (#1140).
    "packages/safe-url/src/safe_url/registrable_domain.py": (2, 2),
    "packages/safe-url/src/safe_url/safe_regex.py": (7, 2),
    "packages/safe-url/src/safe_url/scope_match.py": (1, 1),
    "packages/safe-url/src/safe_url/url_redaction.py": (4, 4),
    # #1137: crawlutron-extract leaf package, stdlib-only, __all__-declared.
    "packages/extract/src/crawlutron_extract/limits.py": (2, 0),
    "packages/extract/src/crawlutron_extract/models.py": (2, 0),
    "packages/extract/src/crawlutron_extract/office.py": (1, 1),
    "packages/extract/src/crawlutron_extract/_extract.py": (2, 1),
    "packages/extract/src/crawlutron_extract/charset.py": (1, 1),
    "packages/extract/src/crawlutron_extract/html_pass.py": (1, 1),
    "packages/extract/src/crawlutron_extract/html_meta.py": (1, 1),
    # #1137: html_diagnostics is the bounded diagnostics door.
    "packages/extract/src/crawlutron_extract/html_diagnostics.py": (1, 1),
    # #1137: shared attr-dict normaliser and URL resolver used by both discovery parsers.
    "packages/extract/src/crawlutron_extract/_html_common.py": (2, 2),
    # #1137: LinkRecord + extract_discovered_links, the bounded link door.
    "packages/extract/src/crawlutron_extract/links.py": (2, 1),
    # #1137: extract_js_endpoints, the bounded JS-endpoint door.
    "packages/extract/src/crawlutron_extract/js_endpoints.py": (1, 1),
    # #1137: extract_sitemap_locs, the bounded sitemap door.
    "packages/extract/src/crawlutron_extract/sitemap.py": (1, 1),
    # #1137: detect_pagination is the bounded next-page door.
    "packages/extract/src/crawlutron_extract/pagination.py": (1, 1),
    "packages/extract/src/crawlutron_extract/text_format.py": (1, 1),
    "packages/extract/src/crawlutron_extract/pdf_pass.py": (1, 1),
}

# Former #215 exemptions: their __all__ deliberately does not enumerate every public
# symbol, so these are counts-only rows pinned at measured counts; only-down from here.
DECLARED_SURFACE_RATCHET = {
    # #643: unregister joins the registry's public door set so test probes can
    # drop their fake owners (registered_owners exhaustiveness must stay true).
    "packages/core/src/crawlutron/runtime/process_state.py": (6, 6),
    # #1137: pre-existing __all__ modules now covered by the derived scan roots
    # (workspace members instead of core-only APP_ROOT).
    "packages/core/src/crawlutron/_time.py": (2, 2),
    "packages/core/src/crawlutron/runtime/admit_sensing.py": (1, 1),
    "packages/crawlutron-admit/src/crawlutron_admit/errors.py": (3, 3),
    "packages/crawlutron-admit/src/crawlutron_admit/gate.py": (2, 2),
    "packages/crawlutron-admit/src/crawlutron_admit/gate_decisions.py": (5, 3),
    "packages/crawlutron-admit/src/crawlutron_admit/gauge.py": (2, 1),
    "packages/crawlutron-admit/src/crawlutron_admit/policy.py": (2, 0),
    "packages/crawlutron-admit/src/crawlutron_admit/seams.py": (2, 2),
    # #1401: pure task-lifetime door (Grace, Handle, NamedRegistry, refusal, TaskScope).
    "packages/crawlutron-admit/src/crawlutron_admit/task_scope.py": (6, 4),
    "packages/crawlutron-admit/src/crawlutron_admit/values.py": (24, 17),
    "packages/sdk/src/crawlutron_sdk/client.py": (2, 2),
    "packages/sdk/src/crawlutron_sdk/errors.py": (18, 2),
    "packages/sdk/src/crawlutron_sdk/models.py": (27, 0),
    "packages/core/src/crawlutron/admission/policy.py": (10, 5),
    "packages/rest/src/crawlutron_rest/api/_depends_singletons.py": (2, 0),
    "packages/core/src/crawlutron/service/async_crawl_runner.py": (3, 2),
    # #664 raised 18,15 -> 19,16 (verify_admin_secret); #822 raised 21,18 -> 22,19
    # (operator_caller: the shared operator identity the keys door and resolver read).
    "packages/core/src/crawlutron/service/cache_sweep.py": (3, 2),
    "packages/core/src/crawlutron/service/sweep_loop.py": (2, 1),
    # Retention sweep (#1076): one pass owns tombstone, orphan-accumulator, and
    # expired-result reclamation plus the WAL checkpoint; hosted on sweep_loop.
    "packages/core/src/crawlutron/service/retention_sweep.py": (4, 2),
    # Spill sweep (#753): one GC lane beside the cache sweep; max_age is wired
    # from the result store's ttl_seconds, not a second literal.
    "packages/core/src/crawlutron/service/spill_sweep.py": (4, 2),
    "packages/core/src/crawlutron/service/store_shutdown.py": (1, 1),
    # #892: the store bootstrap factory + its frozen typed container; the former
    # inline create_app assembly is the library-mode entrance now.
    "packages/core/src/crawlutron/service/store_bootstrap.py": (2, 1),
    # #849: the core moved to service/caller_auth; Request plumbing stays api.
    "packages/rest/src/crawlutron_rest/api/caller_auth.py": (8, 6),
    # #1123: remote_host is the request's remote address -- the admin ladder's
    # per-IP scope, read by the throttle middleware, caller_auth, and route_layer.
    "packages/rest/src/crawlutron_rest/api/credentials.py": (5, 5),
    "packages/core/src/crawlutron/service/caller_concurrency.py": (2, 2),
    # #674 raised 1,1 -> 2,2: CrawlRefusalError is the typed refusal carrier the
    # sweep's deactivation arm reads (reason as an enum member, not boundary text).
    "packages/core/src/crawlutron/service/crawl_executor.py": (2, 2),
    "packages/rest/src/crawlutron_rest/api/crawl_router.py": (4, 3),
    # #656 raised 16,7 -> 18,8: get_concurrency_tracker + TrackerDep make the
    # app-state stash load-bearing (the getattr-None scatter is refused loudly).
    "packages/rest/src/crawlutron_rest/api/dependencies.py": (22, 10),
    # #813/#849: the quota predicate is single-homed here; #1374 moved
    # error_envelope to the shared renderer, lowering this row.
    "packages/rest/src/crawlutron_rest/api/exceptions.py": (9, 7),
    # #668: the single-homed discovery-link constants the 404 details reuse.
    "packages/core/src/crawlutron/discovery/links.py": (3, 0),
    # #649: derived discovery -- one thin re-export and one surface walk.
    "packages/rest/src/crawlutron_rest/discovery/recommended.py": (1, 1),
    "packages/rest/src/crawlutron_rest/discovery/recommended_surface.py": (3, 1),
    "packages/rest/src/crawlutron_rest/discovery/health.py": (1, 1),
    "packages/rest/src/crawlutron_rest/api/router_identity.py": (1, 1),
    "packages/rest/src/crawlutron_rest/api/identity_router.py": (5, 3),
    "packages/core/src/crawlutron/service/job_runner.py": (15, 12),
    # #1224 extraction: absorb_batch is the one public symbol + entry point.
    "packages/core/src/crawlutron/service/job_batch.py": (1, 1),
    "packages/rest/src/crawlutron_rest/api/jobs_router.py": (6, 5),
    # #1083: crawl_tail is the shared crawl tail both doors call (completion
    # metric + result-store put); MCP previously half-tailed.
    "packages/core/src/crawlutron/service/main_helpers.py": (4, 4),
    # #871: lazy app-module reference so service sweeps avoid the fastapi graph.
    "packages/core/src/crawlutron/runtime/app_module_ref.py": (
        6,
        6,
    ),  # raised 3,3 -> 6,6 (#873 reset door + snapshot pair)
    "packages/core/src/crawlutron/service/map_endpoint.py": (2, 1),
    "packages/rest/src/crawlutron_rest/api/metrics_router.py": (2, 1),
    "packages/rest/src/crawlutron_rest/api/middleware.py": (4, 2),
    # #653: the key-verify meter (one class + the shared instance accessor).
    "packages/rest/src/crawlutron_rest/api/admit_router.py": (2, 1),
    # #677: known_reasons is the public door the producer->table loop gate reads.
    "packages/core/src/crawlutron/service/admit_endpoint.py": (3, 2),
    # #941: merged key-verify and anonymous throttle into one rest chokepoint.
    "packages/rest/src/crawlutron_rest/api/throttle.py": (7, 4),
    "packages/core/src/crawlutron/service/policy_defaults.py": (2, 0),
    "packages/rest/src/crawlutron_rest/api/policy_router.py": (3, 1),  # #934 metering adds the module logger
    "packages/rest/src/crawlutron_rest/api/mcp_router.py": (2, 1),
    # RAISED to (14, 12) by #854: the CallerQuotaAdmission port (Protocol, concrete
    # binding, default instance) joins the core so doors consume the interface.
    "packages/core/src/crawlutron/service/caller_auth.py": (14, 12),
    "packages/core/src/crawlutron/service/crawl_endpoint_cores.py": (4, 4),
    "packages/core/src/crawlutron/service/policy_suggestions.py": (3, 2),
    "packages/core/src/crawlutron/service/watch_sweep.py": (4, 2),
    # #849: typed refusals + single-homed quota predicate; #1373 adds the stable
    # quota/validation messages and the one validation_error factory.
    "packages/core/src/crawlutron/service/errors.py": (20, 17),
    # #1374: the one typed renderer shared by REST/MCP/CLI/SDK.
    "packages/core/src/crawlutron/service/error_rendering.py": (5, 2),
    # #1082: admit_webhook_target is the validate-then-admit chokepoint facade.
    "packages/core/src/crawlutron/service/webhooks.py": (16, 14),
    # #668 raised 18,0 -> 19,0: RATE_LIMITED is the JSON-RPC code the anonymous
    # meter's /mcp refusal answers with.
    "packages/mcp/src/crawlutron_mcp/constants.py": (19, 0),
    "packages/mcp/src/crawlutron_mcp/protocol.py": (6, 6),
    # #849: the MCP HTTP router relocated to api/mcp_router (wire concern); the
    # protocol layer imports service only.
    "packages/mcp/src/crawlutron_mcp/server.py": (6, 5),
    # #714: the MCP crawl doors' projection split (projection enum + projected);
    # handlers.py back under the 500-line ceiling.
    "packages/mcp/src/crawlutron_mcp/crawl_shaping.py": (2, 1),
    # #645: tools.py split into registry + handlers (500-line ceiling); the read
    # tools joined. Closures lowered both entry counts.
    "packages/mcp/src/crawlutron_mcp/registry.py": (4, 1),
    # #646/#647/ARCH-1: handlers collapsed; #1374 split tool-result shaping into
    # tool_results.py for the file ceiling.
    "packages/mcp/src/crawlutron_mcp/handlers.py": (5, 3),
    "packages/mcp/src/crawlutron_mcp/tool_results.py": (5, 4),
    # #646: the one control-plane transition both job doors run, plus its shared 404/409 raisers.
    "packages/core/src/crawlutron/service/job_control.py": (3, 3),
    # #647: the typed job cores both doors run (create/list/get/control/delete).
    "packages/core/src/crawlutron/service/job_cores.py": (5, 5),
    # #648: the typed watch cores both doors run (register/list/get/delete).
    "packages/core/src/crawlutron/service/watch_cores.py": (4, 4),
    # #644: the one declaration of the module-level feature routers.
    "packages/rest/src/crawlutron_rest/api/router_registry.py": (1, 0),
    # #664: the auth+metering layer (guards, router factories, ratchet row).
    # #668: PENDING sealed at zero and left; ANONYMOUS_METERED joined (12, 5).
    "packages/rest/src/crawlutron_rest/api/route_layer.py": (12, 5),
    # #818: the fail-closed boot validator for the layer-exempt ratchet.
    "packages/rest/src/crawlutron_rest/api/exempt_route_boot.py": (2, 2),
    # #668: the anonymous IP-bucket meter over the docs surfaces, /mcp
    # anonymous traffic, and the 404 route-miss (metered on the response stage).
    "packages/core/src/crawlutron/service/meter_ledger.py": (2, 2),
    # #668: the shared process-meter skeleton the two throttles compose (the
    # jscpd logic gate's extraction, replacing two hand-copied ledger triples).
    "packages/core/src/crawlutron/service/settings_access.py": (2, 1),
    "packages/core/src/crawlutron/orchestration/admission_stall.py": (6, 4),
    "packages/core/src/crawlutron/orchestration/recursive.py": (9, 6),
    "packages/core/src/crawlutron/orchestration/request_pipeline.py": (5, 3),
    # #1023: the one injected admission governor; a future coordinator swaps in here.
    "packages/core/src/crawlutron/orchestration/resource_governor.py": (4, 4),
    "packages/core/src/crawlutron/transport/robots_parse.py": (6, 6),
    "packages/core/src/crawlutron/orchestration/strategy_chain.py": (4, 2),
    "packages/core/src/crawlutron/runtime/worker_dispatch.py": (1, 1),
    "packages/core/src/crawlutron/runtime/logging_config.py": (1, 1),
    "packages/core/src/crawlutron/runtime/pressure_sizing.py": (13, 9),
    "packages/core/src/crawlutron/search/endpoint.py": (2, 1),
    "packages/core/src/crawlutron/search/providers.py": (21, 9),
    "packages/core/src/crawlutron/search/providers_api.py": (5, 5),
    "packages/core/src/crawlutron/search/providers_html.py": (1, 1),
    "packages/core/src/crawlutron/storage/admin_audit_store.py": (4, 1),
    "packages/core/src/crawlutron/storage/coordination_cookies.py": (2, 1),
    "packages/core/src/crawlutron/storage/coordination_counters.py": (1, 1),
    "packages/core/src/crawlutron/storage/coordination_sync_slots.py": (1, 1),
    "packages/core/src/crawlutron/storage/coordination_store.py": (4, 2),
    "packages/core/src/crawlutron/storage/safe_db.py": (16, 10),  # #1043 + read_txn_depth
    "packages/core/src/crawlutron/storage/coordination_throttle_store.py": (1, 1),
    "packages/core/src/crawlutron/storage/job_store.py": (2, 1),
    "packages/core/src/crawlutron/storage/job_store_collections.py": (11, 6),
    "packages/core/src/crawlutron/storage/job_store_read.py": (7, 4),
    "packages/core/src/crawlutron/storage/job_store_state.py": (2, 1),
    "packages/core/src/crawlutron/runtime/resource_lifecycle.py": (5, 5),
    # (5,5) #643 decline; mapped_value_errors mapping removed (safe_url statelessness)
    "packages/core/src/crawlutron/storage/migrations/runner.py": (5, 4),
    "packages/core/src/crawlutron/storage/migrations/ddl.py": (2, 2),
    "packages/core/src/crawlutron/storage/watch_store.py": (1, 1),
    "packages/core/src/crawlutron/transport/browser.py": (3, 2),
    "packages/core/src/crawlutron/transport/browser_cookies.py": (6, 3),
    "packages/core/src/crawlutron/transport/browser_result.py": (1, 1),
    "packages/core/src/crawlutron/transport/content_pipeline.py": (6, 4),
    "packages/core/src/crawlutron/transport/http_client_pool.py": (1, 1),
    "packages/core/src/crawlutron/transport/strategy_request.py": (1, 0),
    # Issue #710: crawl CLI facade. Counts derived by running the gate, not
    # predicted.
    "packages/rest/src/crawlutron_rest/cli/__main__.py": (11, 2),
    "packages/rest/src/crawlutron_rest/cli/client.py": (3, 1),
    "packages/rest/src/crawlutron_rest/cli/docker.py": (11, 1),
    "packages/rest/src/crawlutron_rest/cli/output.py": (1, 1),
}
