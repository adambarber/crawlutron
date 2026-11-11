"""App-owned store teardown: shutdown closes what create_app opened (#838)."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

_STORE_ATTRS = (
    "job_store",
    "result_store",
    "spill_store",
    "watch_store",
    "caller_key_store",
    "audit_store",
)

__all__ = ["close_app_stores"]


async def close_app_stores(app: FastAPI) -> None:
    """Close the stores create_app built: their connections are shutdown's.

    The process-wide module singleton is excluded: its connections are the
    process's, so one test's shutdown must not strand later tests' apps.
    """
    from crawlutron.main import module_app

    module_state = module_app()
    module_state_state = module_state.state if module_state is not None else None
    for attr in _STORE_ATTRS:
        store = getattr(app.state, attr, None)
        if store is not None and store is not getattr(module_state_state, attr, None):
            store.close()
