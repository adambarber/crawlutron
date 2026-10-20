"""The second half of the private-import ledger, N-Z per module key."""

from __future__ import annotations

from tests._private_import_decl import (
    _APP_INTERNAL_WELD,
    Declaration,
)

DECLARED: dict[str, Declaration] = {
    "app.orchestration.recursive._RecursiveState": _APP_INTERNAL_WELD,
    "app.orchestration.recursive._commit_recursive_results": _APP_INTERNAL_WELD,
    "app.orchestration.recursive._crawl_recursive_batch": _APP_INTERNAL_WELD,
    "app.orchestration.recursive._spill_write": _APP_INTERNAL_WELD,
    "app.orchestration.request_pipeline._PipelineDependencies": _APP_INTERNAL_WELD,
    "app.orchestration.request_pipeline._PipelineState": _APP_INTERNAL_WELD,
    "app.orchestration.spill_sink._SpillSink": _APP_INTERNAL_WELD,
}
