"""The first half of the private-import ledger, A-M per module key."""

from __future__ import annotations

from tests._private_import_decl import (
    _INSTANCE_STATE,
    _SUBSTITUTED_COLLABORATOR,
    Declaration,
)

DECLARED: dict[str, Declaration] = {
    "app.api.async_crawl_runner._build_async_crawl_semaphore": _SUBSTITUTED_COLLABORATOR,
    "app.transport.content_cache.ContentCache._index": _INSTANCE_STATE,
    "app.transport.content_cache.ContentCache._lock": _INSTANCE_STATE,
    "app.transport.content_cache.ContentCache._index_sizes": _INSTANCE_STATE,
    "app.transport.content_cache.ContentCache._index_cached_at": _INSTANCE_STATE,
    "app.transport.content_cache.ContentCache._memory_cache": _INSTANCE_STATE,
    "app.transport.content_cache.ContentCache._path_for": _INSTANCE_STATE,
    "app.transport.content_cache.ContentCache._drop_locked": _INSTANCE_STATE,
    "app.transport.content_cache.ContentCache._unlink_doomed": _INSTANCE_STATE,
    "app.transport.content_cache.ContentCache._read_envelope": _INSTANCE_STATE,
    "app.transport.content_cache.ContentCache._MEMORY_CACHE_MAX_SIZE": _INSTANCE_STATE,
    "app.transport.cookie_jar.CookieJar._inmemory": _INSTANCE_STATE,
    "app.transport.browser_pool.BrowserPool._available": _INSTANCE_STATE,
    "app.transport.browser_pool.BrowserPool._pool_size": _INSTANCE_STATE,
    "app.transport.browser_pool.BrowserPool._renderers": _INSTANCE_STATE,
    "app.transport.cache.HttpCache._data": _INSTANCE_STATE,
    "app.runtime.memory_pressure.MemoryGate._lag_probe": _INSTANCE_STATE,
    "app.runtime.resource_pressure.LoopLagProbe._lag": _INSTANCE_STATE,
    "app.runtime.resource_pressure.LoopLagProbe._task": _INSTANCE_STATE,
    "app.runtime.resource_pressure.ResourceGate._lag_probe": _INSTANCE_STATE,
    "app.runtime.resource_pressure.WeightedAdmission._p": _INSTANCE_STATE,
    "app.storage.job_store.JobStore._conn": _INSTANCE_STATE,
}
