"""The watch sweep: one pass rechecks every due watch and announces changes.

A check is a conditional GET under the HTTP strategy alone -- only a delivered
representation counts; no verdict is an ``ERROR``, decided here, not in the store.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

from app.api.crawl_executor import execute_crawl
from app.api.webhooks import post_webhook
from app.config.settings import CrawlerSettings
from app.models.crawl_policy import CrawlPolicy
from app.models.crawl_request import CrawlRequest
from app.models.fetch_validators import FetchValidators
from app.models.host_policy import HostPolicy
from app.models.watch_announcement import WatchAnnouncement
from app.models.watch_outcome import WatchOutcome
from app.models.watch_state import WatchState
from app.storage.watch_store import WatchStore
from app.vocabulary.strategies import HTTP_DECLARED_CRAWLER
from app.vocabulary.taxonomy import CrawlClassification

logger = logging.getLogger(__name__)

WATCH_SWEEP_INTERVAL_SECONDS = 60.0
"""How often the process sweeps the registry.

A bound on how late a check runs, nothing more: each watch's own
``interval_seconds`` decides when it is due, so raising this cadence trades
scheduler latency for idle passes over an empty table, and lowering it never
checks a watch early.
"""

__all__ = ["WATCH_SWEEP_INTERVAL_SECONDS", "WatchSweeper", "sweep_watches_forever"]


def _check_policy(owner: str = "") -> CrawlPolicy:
    """The single-URL policy a check runs under: HTTP alone. ``owner`` sets the
    cookie namespace so a keyed watch inherits that caller's cookies; the model
    constructor is the boundary, and ``execute_crawl`` resolves it from there.
    """
    policy: dict[str, Any] = {"strategies": [HTTP_DECLARED_CRAWLER], "limits": {}}
    if owner:
        policy["request_profile"] = {"cookie_namespace": owner}
    return CrawlPolicy.model_validate(policy)


def _announcement(watch: WatchState, *, outcome: WatchOutcome, checked_at: float) -> WatchAnnouncement:
    """This watch's notification for one pass."""
    return {
        "watch_id": watch.watch_id,
        "url": watch.url,
        "interval_seconds": watch.interval_seconds,
        "outcome": outcome,
        "checked_at": checked_at,
    }


class WatchSweeper:
    """Rechecks due watches and announces changes, one pass at a time; the store
    and policy are constructor arguments, not module globals, so a watch under
    a since-closed allow-list fails rather than silently reaching the host.
    """

    def __init__(
        self,
        store: WatchStore,
        *,
        host_policy: HostPolicy,
        settings: CrawlerSettings,
        shutdown_event: asyncio.Event | None = None,
        key_store=None,
    ) -> None:
        self._store = store
        self._host_policy = host_policy
        self._settings = settings
        self._shutdown_event = shutdown_event
        self._key_store = key_store

    async def sweep_once(self, now: float) -> list[WatchOutcome]:
        """Recheck every watch due at ``now``; one conclusion per due watch, in
        registry order. A raising watch is isolated per-watch so it cannot starve
        the watches sorted after it in the due list (#228).
        """
        outcomes: list[WatchOutcome] = []
        for watch in await self._store.adue(now):
            revoked = (
                self._key_store is not None
                and watch.owner
                and not await self._key_store.aexists_key(watch.owner)
            )
            if revoked:
                # A revoked owner's watch must not issue checks under a dead
                # identity: deactivate it, terminal, and move on (#207).
                logger.warning(
                    "watch_owner_revoked_deactivated",
                    extra={"watch_id": watch.watch_id, "owner": watch.owner},
                )
                await self._store.adeactivate(
                    watch.watch_id, now=now, next_check_at=now + watch.interval_seconds
                )
                outcomes.append(WatchOutcome.ERROR)
                continue
            try:
                outcome = await self._check(watch, now=now)
            except Exception:
                # One watch that raises must not end the pass nor starve its
                # co-scheduled watches: record the failure and reschedule anyway.
                logger.exception(
                    "watch_check_failed",
                    extra={"watch_id": watch.watch_id, "url": watch.url},
                )
                try:
                    await self._store.arecord_check(
                        watch.watch_id,
                        outcome=WatchOutcome.ERROR,
                        now=now,
                        next_check_at=now + watch.interval_seconds,
                    )
                except Exception:
                    logger.exception("watch_check_record_failed", extra={"watch_id": watch.watch_id})
                outcomes.append(WatchOutcome.ERROR)
            else:
                outcomes.append(outcome)
        return outcomes

    async def _check(self, watch: WatchState, *, now: float) -> WatchOutcome:
        outcome, etag, last_modified = await self._revalidate(watch)
        await self._store.arecord_check(
            watch.watch_id,
            outcome=outcome,
            now=now,
            next_check_at=now + watch.interval_seconds,
            etag=etag,
            last_modified=last_modified,
        )
        if outcome is WatchOutcome.MODIFIED and watch.webhook_url:
            await post_webhook(
                watch.webhook_url,
                _announcement(watch, outcome=outcome, checked_at=now),
                host_policy=self._host_policy,
            )
        return outcome

    async def _revalidate(self, watch: WatchState) -> tuple[WatchOutcome, str | None, str | None]:
        """One conditional check: stored validators in, the row's new ones out.
        A ``304``/failure keeps the stored set; a delivered body returns the
        origin's new etag. ``execute_crawl`` re-admits and resolves policy.
        """
        validators: dict[str, FetchValidators] = {}
        if watch.validators.etag or watch.validators.last_modified:
            validators[watch.url] = FetchValidators(
                if_none_match=watch.validators.etag,
                if_modified_since=watch.validators.last_modified,
            )
        payload = CrawlRequest(
            url=watch.url,
            policy=_check_policy(watch.owner),
            validators=validators,
        )
        envelope = await execute_crawl(
            payload,
            settings=self._settings,
            shutdown_event=self._shutdown_event,
            urls=[watch.url],
        )
        if not envelope["results"]:
            logger.warning("watch_check_produced_no_result", extra={"watch_id": watch.watch_id})
            return WatchOutcome.ERROR, None, None
        result = envelope["results"][0]
        if result.classification == CrawlClassification.not_modified:
            return WatchOutcome.NOT_MODIFIED, None, None
        if result.status != "success":
            return WatchOutcome.ERROR, None, None
        return (
            WatchOutcome.MODIFIED,
            result.validators.get("etag"),
            result.validators.get("last_modified"),
        )


async def sweep_watches_forever(
    store: WatchStore,
    *,
    host_policy: HostPolicy,
    settings: CrawlerSettings,
    shutdown_event: asyncio.Event,
    interval_seconds: float = WATCH_SWEEP_INTERVAL_SECONDS,
    clock: Callable[[], float] = time.time,
    key_store=None,
) -> None:
    """Sweep the registry until told to stop; the wait rides the shutdown event
    so a stopping process leaves at once. ``key_store`` answers the per-owner
    liveness a revoked-owner watch needs (#207).
    """
    sweeper = WatchSweeper(
        store,
        host_policy=host_policy,
        settings=settings,
        shutdown_event=shutdown_event,
        key_store=key_store,
    )
    while not shutdown_event.is_set():
        try:
            await sweeper.sweep_once(now=clock())
        except Exception:
            # One watch that makes the crawl raise must not end the sweep: the alternative is
            # a registry that quietly stops being checked, with nothing reporting a dead task.
            logger.exception("watch_sweep_pass_failed")
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=interval_seconds)
        except TimeoutError:
            continue
