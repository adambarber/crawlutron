"""Store bootstrap: the embedder-facing store construction entrance.

The factory derives the shared database path from settings unless overridden,
builds the six stores, and wires the key-store -> audit sink in one call.
"""

from __future__ import annotations

import dataclasses

from safe_url.host_policy import HostPolicy

from crawlutron.admission.policy import PolicyTier
from crawlutron.config.env import get_env_float, get_env_int
from crawlutron.config.settings import CrawlerSettings
from crawlutron.storage import (
    admin_audit_store as audit_store_mod,
)
from crawlutron.storage import (
    caller_key_store as key_store_mod,
)
from crawlutron.storage import (
    job_store as job_store_mod,
)
from crawlutron.storage import (
    result_store as result_store_mod,
)
from crawlutron.storage import (
    watch_store as watch_store_mod,
)


@dataclasses.dataclass(frozen=True)
class Stores:
    """The six bound store handles a crawler service instance owns."""

    job_store: job_store_mod.JobStore
    result_store: result_store_mod.CrawlResultStore
    spill_store: result_store_mod.CrawlSpillStore
    watch_store: watch_store_mod.WatchStore
    caller_key_store: key_store_mod.CallerKeyStore
    audit_store: audit_store_mod.AdminAuditStore


__all__ = ["Stores", "build_stores"]


def build_stores(settings: CrawlerSettings, database_path: str | None = None) -> Stores:
    """Derive the database path, construct the six stores, wire the audit sink."""
    db = database_path if database_path is not None else settings.cache_dir + "/crawler.db"
    job_store = job_store_mod.JobStore(
        db,
        policy=HostPolicy.from_settings(settings),
        tier=PolicyTier.from_settings(settings),
    )
    result_store = result_store_mod.CrawlResultStore(
        db,
        ttl_seconds=get_env_float("CRAWLUTRON_RESULT_TTL_SECONDS", 3600.0),
        max_entries=get_env_int("CRAWLUTRON_RESULT_MAX_ENTRIES", 1000),
    )
    spill_store = result_store_mod.CrawlSpillStore(db)
    watch_store = watch_store_mod.WatchStore(db)
    caller_key_store = key_store_mod.CallerKeyStore(db)
    audit_store = audit_store_mod.AdminAuditStore(db)
    caller_key_store.attach_audit(audit_store)
    return Stores(
        job_store=job_store,
        result_store=result_store,
        spill_store=spill_store,
        watch_store=watch_store,
        caller_key_store=caller_key_store,
        audit_store=audit_store,
    )
