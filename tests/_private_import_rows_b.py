"""The second half of the private-import ledger, N-Z per module key."""

from __future__ import annotations

from tests._private_import_decl import (
    _CHOKEPOINT_STATE,
    _LIFECYCLE_DRAIN_PENDING_CLIENT_CLOSES,
    _SUBSTITUTED_COLLABORATOR,
    Declaration,
    _d,
)

DECLARED: dict[str, Declaration] = {
    "app.transport.pooled_clients._drain_pending_client_closes": _d(
        _LIFECYCLE_DRAIN_PENDING_CLIENT_CLOSES.kind,
        _LIFECYCLE_DRAIN_PENDING_CLIENT_CLOSES.reason,
        ["app"],
    ),
    "app.transport.pooled_clients._NoStoreCookieJar": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.transport.fetchers._release_stream_noblock": _SUBSTITUTED_COLLABORATOR,
    "app.transport.pinned_transport._http_413": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["app"],
    ),
    "app.transport.pinned_transport._redirect_target": _d(
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
