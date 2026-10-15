"""The second half of the private-import ledger, N-Z per module key."""

from __future__ import annotations

from tests._private_import_decl import (
    _CHOKEPOINT_STATE,
    _LIFECYCLE_DRAIN_PENDING_CLIENT_CLOSES,
    _OWNED_POOL_MACHINERY,
    _PRIVATE_TYPE,
    _SUBSTITUTED_COLLABORATOR,
    Declaration,
    _d,
)

DECLARED: dict[str, Declaration] = {
    "app.transport.document._extract_pdf_bytes_to_markdown": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.transport.document._format_table": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.transport.document._pdf_to_markdown": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.transport.fetch_policy._RETRY_BACKOFF_CAP_SECONDS": _d(
        _CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]
    ),
    "app.transport.fetchers._cffi_fetch": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.transport.fetchers._release_stream_noblock": _SUBSTITUTED_COLLABORATOR,
    "app.transport.pooled_clients._LoopBoundPooledClient": _d(
        _PRIVATE_TYPE.kind, _PRIVATE_TYPE.reason, ["test"]
    ),
    "app.transport.pooled_clients._NoStoreCookieJar": _d(_PRIVATE_TYPE.kind, _PRIVATE_TYPE.reason, ["test"]),
    "app.transport.pooled_clients._PENDING_CLIENT_CLOSE": _d(
        _CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]
    ),
    "app.transport.pooled_clients._PENDING_CLIENT_CLOSE_LOCK": _d(
        _CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]
    ),
    "app.transport.pooled_clients._PENDING_CLIENT_CLOSE_MAX": _d(
        _CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]
    ),
    "app.transport.pooled_clients._build_async_client": _SUBSTITUTED_COLLABORATOR,
    "app.transport.pooled_cffi._build_cffi_session": _SUBSTITUTED_COLLABORATOR,
    "app.transport.pooled_clients._drain_pending_client_closes": _d(
        _LIFECYCLE_DRAIN_PENDING_CLIENT_CLOSES.kind,
        _LIFECYCLE_DRAIN_PENDING_CLIENT_CLOSES.reason,
        ["test", "app"],
    ),
    "app.transport.pooled_clients._queue_superseded_close": _SUBSTITUTED_COLLABORATOR,
    "app.transport.pooled_clients._release_dropped_client": _OWNED_POOL_MACHINERY,
    "app.transport.pooled_clients._schedule_client_close": _OWNED_POOL_MACHINERY,
    "app.transport.pinned_transport._CREDENTIAL_HEADERS": _d(
        _CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]
    ),
    "app.transport.pinned_transport._DNS_CACHE": _d(
        _CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]
    ),
    "app.transport.pinned_transport._DNS_CACHE._lock": _d(
        _CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]
    ),
    "app.transport.pinned_transport._MAX_REDIRECT_HOPS": _d(
        _CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]
    ),
    "app.transport.pinned_transport._declared_length_exceeds": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test", "app"],
    ),
    "app.transport.pinned_transport._http_413": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["app"],
    ),
    "app.transport.pinned_transport._is_private_or_loopback_ip": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.transport.pinned_transport._redirect_target": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.transport.pinned_transport._resolve_ips": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.transport.request_profile._apply_credentials": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.transport.request_profile._cookie_header_for_url": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.transport.request_profile._select_fingerprint": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.orchestration.strategy_chain._starting_strategies": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.runtime.logging_config._BC_OWNED_HANDLER": _d(
        _CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]
    ),
    "app.main._warn_if_bound_off_loopback": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.models.registrable_domain._PSL": _d(_CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]),
    "app.search.providers._provider_http": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
}
