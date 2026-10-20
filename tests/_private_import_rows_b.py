"""The second half of the private-import ledger, N-Z per module key."""

from __future__ import annotations

from tests._private_import_decl import (
    _APP_INTERNAL_WELD,
    _LIFECYCLE_DRAIN_PENDING_CLIENT_CLOSES,
    Declaration,
    _d,
)

DECLARED: dict[str, Declaration] = {
    "app.transport.pooled_clients._drain_pending_client_closes": _d(
        _LIFECYCLE_DRAIN_PENDING_CLIENT_CLOSES.kind,
        _LIFECYCLE_DRAIN_PENDING_CLIENT_CLOSES.reason,
        ["app"],
    ),
    "app.transport.pinned_transport._http_413": _d(
        "implementation step",
        "Builds the too-large 413 HttpFetchResult verdict fetchers share within the transport package.",
        ["app"],
    ),
    "app.mcp.tools._http_exception_message": _APP_INTERNAL_WELD,
    "app.orchestration.recursive._RecursiveState": _APP_INTERNAL_WELD,
    "app.orchestration.recursive._commit_recursive_results": _APP_INTERNAL_WELD,
    "app.orchestration.recursive._crawl_recursive_batch": _APP_INTERNAL_WELD,
    "app.orchestration.recursive._spill_write": _APP_INTERNAL_WELD,
    "app.orchestration.request_pipeline._PipelineDependencies": _APP_INTERNAL_WELD,
    "app.orchestration.request_pipeline._PipelineState": _APP_INTERNAL_WELD,
    "app.orchestration.spill_sink._SpillSink": _APP_INTERNAL_WELD,
    "app.transport.http_cache._not_modified_result": _APP_INTERNAL_WELD,
    "app.transport.http_cache._resolve_http_limits": _APP_INTERNAL_WELD,
    "app.transport.http_cache._revalidate_content_cache": _APP_INTERNAL_WELD,
    "app.transport.http_cache._store_http_caches": _APP_INTERNAL_WELD,
    "app.transport.http_classify._classify_extracted_content": _APP_INTERNAL_WELD,
    "app.transport.http_classify._extract_http_phase": _APP_INTERNAL_WELD,
    "app.transport.http_classify._request_profile_diagnostics": _APP_INTERNAL_WELD,
    "app.transport.pooled_clients._bind_to_running_loop": _APP_INTERNAL_WELD,
    "app.transport.pooled_clients._discard_if_unowned_loop": _APP_INTERNAL_WELD,
    "app.transport.pooled_clients._LoopBoundPooledClient": _APP_INTERNAL_WELD,
}
