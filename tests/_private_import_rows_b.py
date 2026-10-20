"""The second half of the private-import ledger, N-Z per module key."""

from __future__ import annotations

from tests._private_import_decl import (
    _APP_INTERNAL_WELD,
    _CHOKEPOINT_STATE,
    _LIFECYCLE_DRAIN_PENDING_CLIENT_CLOSES,
    _SUBSTITUTED_COLLABORATOR,
    Declaration,
    _d,
)

DECLARED: dict[str, Declaration] = {
    "app.search.providers._ApiKeyProvider": _d(
        "private type",
        "#128 folding test subclasses the shared keyed base to prove the entry is folded.",
        ["test"],
    ),
    "app.search.providers._ProviderCall": _d(
        "private type",
        "#128 folding test builds the minimal provider's request via the shared call shape.",
        ["test"],
    ),
    "app.transport.pooled_clients._drain_pending_client_closes": _d(
        _LIFECYCLE_DRAIN_PENDING_CLIENT_CLOSES.kind,
        _LIFECYCLE_DRAIN_PENDING_CLIENT_CLOSES.reason,
        ["app"],
    ),
    "app.transport.fetchers._release_stream_noblock": _SUBSTITUTED_COLLABORATOR,
    "app.transport.pinned_transport._http_413": _d(
        "implementation step",
        "Builds the too-large 413 HttpFetchResult verdict fetchers share within the transport package.",
        ["app"],
    ),
    "app.models.registrable_domain._PSL": _d(_CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]),
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
