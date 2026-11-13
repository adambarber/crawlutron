"""The watch sweep: one pass rechecks every due watch and announces changes.

A check is a conditional GET under the HTTP strategy alone -- only a delivered
representation counts; no verdict is an ``ERROR``, decided here, not in the store.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from crawlutron.config.settings import CrawlerSettings
from crawlutron.models.caller_identity import CallerIdentity
from crawlutron.models.crawl_policy import CrawlPolicy
from crawlutron.models.crawl_policy_limits import CrawlPolicyLimits
from crawlutron.models.crawl_request import CrawlRequest
from crawlutron.models.crawl_strategy import CrawlStrategy
from crawlutron.models.fetch_validators import FetchValidators
from crawlutron.models.host_policy import HostPolicy
from crawlutron.models.watch_announcement import WatchAnnouncement
from crawlutron.models.watch_state import WatchState
from crawlutron.service.caller_auth import Tracking, admit, anonymous_caller, identity_for_key
from crawlutron.service.crawl_executor import CrawlRefusalError, execute_crawl
from crawlutron.service.errors import is_quota_refusal
from crawlutron.service.webhooks import start_webhook_delivery
from crawlutron.storage.watch_store import WatchStore
from crawlutron.transport.freshness import default_clock
from crawlutron.vocabulary.strategies import HTTP_DECLARED_CRAWLER
from crawlutron.vocabulary.taxonomy import CrawlClassification, status_for_classification

if TYPE_CHECKING:
    from crawlutron.service.caller_concurrency import CallerConcurrencyTracker
    from crawlutron.storage.job_store import JobStore

logger = logging.getLogger(__name__)

WATCH_SWEEP_INTERVAL_SECONDS = 60.0

# How long a sweep's claim on a due watch stays fresh: a sweeper that died
# mid-check releases the row to the next pass after its LAST live touch.
_CLAIM_SECONDS = 300.0
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
    return CrawlPolicy(
        strategies=[CrawlStrategy(HTTP_DECLARED_CRAWLER)],
        limits=CrawlPolicyLimits(),
        request_profile={"cookie_namespace": owner} if owner else {},
    )


def _announcement(watch: WatchState, *, outcome: CrawlClassification, checked_at: float) -> WatchAnnouncement:
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
        concurrency_tracker: CallerConcurrencyTracker | None = None,
        job_store: JobStore | None = None,
        clock: Callable[[], float] = default_clock,
    ) -> None:
        self._store = store
        self._host_policy = host_policy
        self._settings = settings
        self._shutdown_event = shutdown_event
        self._key_store = key_store
        self._concurrency_tracker = concurrency_tracker
        self._job_store = job_store
        self._claim_token = f"{id(self)}-{time.monotonic_ns()}"
        self._clock = clock

    async def sweep_once(self, now: float) -> list[CrawlClassification]:
        """Recheck every watch due at ``now``; one conclusion per due watch, in
        registry order. A raising watch is isolated per-watch so it cannot starve
        the watches sorted after it in the due list.
        """
        outcomes: list[CrawlClassification] = []
        # One claim per pass: a second sweeper's CAS on these rows loses and
        # skips them, so two sweeps never double-fire a webhook.
        due = await self._store.aclaim_due(now, self._claim_token, claim_seconds=_CLAIM_SECONDS)
        for watch in due:
            outcome = await self._one_watch(watch, now=now)
            if outcome is not None:
                outcomes.append(outcome)
        return outcomes

    async def _one_watch(self, watch: WatchState, *, now: float) -> CrawlClassification | None:
        """One due watch's conclusion, isolated: a raising watch neither ends the
        pass nor starves watches sorted after it. The lease is re-stamped at the
        check's start; a lost renewal (a second sweeper re-claimed it) skips.
        """
        if not await self._store.arenew_claim(watch.watch_id, self._claim_token, now=self._clock()):
            # Ownership was already lost -- working the row would be the
            # duplicate fire the claim exists to prevent.
            return None
        revoked = (
            self._key_store is not None and watch.owner and not await self._key_store.aexists_key(watch.owner)
        )
        if revoked:
            # A revoked owner's watch must not issue checks under a dead
            # identity: deactivate it, terminal, and move on.
            logger.warning(
                "watch_owner_revoked_deactivated",
                extra={"watch_id": watch.watch_id, "owner": watch.owner},
            )
            await self._store.adeactivate(
                watch.watch_id,
                now=now,
                next_check_at=now + watch.interval_seconds,
                claimed_by=self._claim_token,
            )
            return CrawlClassification.site_error
        try:
            outcome = await self._check(watch, now=now)
        except CrawlRefusalError as error:
            return await self._deactivate_refused(watch, error.reason, now=now)
        except Exception as error:
            if is_quota_refusal(error):
                # The owner's own budget refused the check: it never ran, so
                # no failure streak -- leave the row to the next sweep.
                logger.info("watch_check_quota_refused", extra={"watch_id": watch.watch_id})
                return None
            # One watch that raises must not end the pass nor starve its
            # co-scheduled watches: record the failure and reschedule anyway.
            logger.exception(
                "watch_check_failed",
                extra={"watch_id": watch.watch_id, "url": watch.redacted_url()},
            )
            try:
                recorded = await self._store.arecord_check(
                    watch.watch_id,
                    outcome=CrawlClassification.unknown_error,
                    now=now,
                    next_check_at=now + watch.interval_seconds,
                    claimed_by=self._claim_token,
                )
                if not recorded:
                    logger.info("watch_check_stale_verdict_dropped", extra={"watch_id": watch.watch_id})
            except Exception:
                logger.exception("watch_check_record_failed", extra={"watch_id": watch.watch_id})
            return CrawlClassification.unknown_error
        return outcome

    async def _deactivate_refused(
        self, watch: WatchState, reason: CrawlClassification, *, now: float
    ) -> CrawlClassification:
        """A watch the live policy no longer admits is terminal: re-admitting
        every interval burns a full admit cycle for the same verdict. Same
        lifecycle as a revoked owner: deactivate with the reason recorded.
        """
        logger.warning(
            "watch_admission_refused_deactivated",
            extra={"watch_id": watch.watch_id, "url": watch.redacted_url(), "reason": reason.value},
        )
        await self._store.adeactivate(
            watch.watch_id,
            now=now,
            next_check_at=now + watch.interval_seconds,
            reason=reason,
            claimed_by=self._claim_token,
        )
        return reason

    async def _owner_identity(self, owner: str) -> CallerIdentity:
        """The owner as a caller identity, so a check is metered against the owner's
        own ``max_concurrency`` budget; missing store/owner falls back to anonymous.
        """
        key_store = self._key_store
        if owner and key_store is not None:
            record = await key_store.aget_for_id(owner)
            if record is not None:
                return identity_for_key(record)
        return anonymous_caller()

    async def _check(self, watch: WatchState, *, now: float) -> CrawlClassification:
        identity = await self._owner_identity(watch.owner)
        async with admit(
            identity,
            1,
            tracking=Tracking(
                job_store=self._job_store,
                concurrency_tracker=self._concurrency_tracker,
            ),
        ):
            classification, etag, last_modified = await self._revalidate(watch)
        await self._store.arecord_check(
            watch.watch_id,
            outcome=classification,
            now=now,
            next_check_at=now + watch.interval_seconds,
            etag=etag,
            last_modified=last_modified,
            claimed_by=self._claim_token,
        )
        # A robots-txt refusal is terminal: stop sweeping it rather than re-checking
        # a host that declined us forever (same lifecycle as a revoked owner).
        if status_for_classification(classification) == "skipped":
            await self._store.adeactivate(
                watch.watch_id,
                now=now,
                next_check_at=now + watch.interval_seconds,
                reason=classification,
                claimed_by=self._claim_token,
            )
            return classification
        # A delivered change is what a webhook exists to say: any success-status
        # classification except "not_modified" (target RE-admitted there).
        if (
            status_for_classification(classification) == "success"
            and classification != CrawlClassification.not_modified
            and watch.webhook_url is not None
        ):
            try:
                target = watch.target(self._host_policy)
            except ValueError:
                logger.warning(
                    "watch_webhook_admission_failed",
                    extra={"watch_id": watch.watch_id, "url": watch.redacted_webhook_url()},
                )
                return classification
            if target is None:
                return classification
            # Delivery atomicity rides the RESCHEDULE above: record_check nulls the
            # claim and pushes next_check_at out, so no sweeper re-claims mid-delivery.
            if not await self._store.arenew_claim(watch.watch_id, self._claim_token, now=self._clock()):
                return classification
            # Backgrounded, not awaited: a slow receiver must not stall the pass;
            # the delivery rides the tracked set and shutdown drains it.
            start_webhook_delivery(
                target,
                _announcement(watch, outcome=classification, checked_at=now),
            )
        return classification

    async def _revalidate(self, watch: WatchState) -> tuple[CrawlClassification, str | None, str | None]:
        """One conditional check: stored validators in, the row's new ones out.
        A ``304``/failure keeps the stored set; a delivered body returns the
        origin's etag. The classification is read off the result directly.
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
        )
        if not envelope["results"]:
            logger.warning("watch_check_produced_no_result", extra={"watch_id": watch.watch_id})
            return CrawlClassification.unknown_error, None, None
        result = envelope["results"][0]
        # The result classification IS the outcome, read directly: not_modified,
        # a success classification for a delivered 200, or an error for failure.
        return (
            CrawlClassification(result.classification),
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
    clock: Callable[[], float] = default_clock,
    key_store=None,
    concurrency_tracker: CallerConcurrencyTracker | None = None,
    job_store: JobStore | None = None,
) -> None:
    """Sweep the registry until told to stop; the wait rides the shutdown event
    so a stopping process leaves at once. ``key_store`` answers revoked-owner
    liveness; the tracker/store meter checks against the owner.
    """
    sweeper = WatchSweeper(
        store,
        host_policy=host_policy,
        settings=settings,
        shutdown_event=shutdown_event,
        key_store=key_store,
        concurrency_tracker=concurrency_tracker,
        job_store=job_store,
        clock=clock,
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
