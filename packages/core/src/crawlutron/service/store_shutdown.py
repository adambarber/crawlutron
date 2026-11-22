"""App-owned store teardown: shutdown closes what create_app opened (#838, #923)."""

from __future__ import annotations

from typing import Protocol


class _StoreCarrier(Protocol):
    @property
    def state(self) -> object: ...


_STORE_ATTRS = (
    "job_store",
    "result_store",
    "spill_store",
    "watch_store",
    "caller_key_store",
    "audit_store",
)

#: create_app also opens the concurrency tracker's coordination-store
#: connection; teardown must close it or it outlives store teardown (#1092).
_TRACKER_ATTR = "concurrency_tracker"

__all__ = ["close_app_stores"]


async def close_app_stores(app: _StoreCarrier) -> None:
    """Close the stores create_app built; a second close is a sqlite3 no-op.

    Every store on ``app.state`` is the app's own, so shutdown closes them all,
    the cookie jar's shared store included -- even when state is the singleton's.
    """
    for attr in _STORE_ATTRS:
        store = getattr(app.state, attr, None)
        if store is not None:
            store.close()
    cookie_jar = getattr(app.state, "cookie_jar", None)
    cookie_store = getattr(cookie_jar, "store", None)
    if cookie_store is not None:
        cookie_store.close()
    tracker = getattr(app.state, _TRACKER_ATTR, None)
    if tracker is not None:
        tracker.close()
