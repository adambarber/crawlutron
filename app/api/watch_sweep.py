"""The watch sweep: one pass rechecks every due watch and announces changes.

A check is a conditional GET under the HTTP strategy alone -- only a delivered
representation counts; no verdict is an ``ERROR``, decided here, not in the store.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from app.admission.policy import read_policy
from app.api.crawl_executor import execute_crawl
from app.api.webhooks import post_webhook
from app.config.settings import CrawlerSettings
from app.models.content_validators import ContentValidators
from app.models.fetch_validators import FetchValidators
from app.models.host_policy import HostPolicy
from app.models.normalized_url import NormalizedUrl
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


def _check_policy():
    """The single-URL policy a check runs under: HTTP alone, declared here, so a
    climb to a browser would render a body the caller never received and need a
    pool that is not part of the answer.
    """
    return read_policy({"strategies": [HTTP_DECLARED_CRAWLER], "limits": {}, "headers": {}})


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
    ) -> None:
        self._store = store
        self._host_policy = host_policy
        self._settings = settings
        self._shutdown_event = shutdown_event

    async def sweep_once(self, now: float) -> list[WatchOutcome]:
        """Recheck every watch due at ``now``; return what each pass concluded.

        One conclusion per due watch, in registry order -- the caller reports
        progress from this, while the stored row is the record of what happened.
        """
        return [await self._check(watch, now=now) for watch in await self._store.adue(now)]

    async def _check(self, watch: WatchState, *, now: float) -> WatchOutcome:
        outcome, validators = await self._revalidate(watch)
        await self._store.arecord_check(
            watch.watch_id,
            outcome=outcome,
            now=now,
            next_check_at=now + watch.interval_seconds,
            validators=validators,
        )
        if outcome is WatchOutcome.MODIFIED and watch.webhook_url:
            await post_webhook(
                watch.webhook_url,
                _announcement(watch, outcome=outcome, checked_at=now),
                host_policy=self._host_policy,
            )
        return outcome

    async def _revalidate(self, watch: WatchState) -> tuple[WatchOutcome, ContentValidators]:
        """One conditional check: stored validators in, the row's new ones out.

        A ``304`` and a failure both keep what was already known; a delivered
        body replaces the set outright, dropping a no-longer-sent validator.
        """
        try:
            NormalizedUrl(watch.url, policy=self._host_policy)
        except ValueError:
            logger.warning(
                "watch_no_longer_admitted",
                extra={"watch_id": watch.watch_id, "url": watch.url},
            )
            return WatchOutcome.ERROR, watch.validators

        envelope = await execute_crawl(
            {
                "url": watch.url,
                "policy": _check_policy(),
                # dumped rather than hand-spelled: the keys stay the spellings the
                # models and the pipeline already agree on.
                "validators": {
                    watch.url: FetchValidators(
                        if_none_match=watch.validators.etag,
                        if_modified_since=watch.validators.last_modified,
                    ).model_dump()
                },
            },
            settings=self._settings,
            shutdown_event=self._shutdown_event,
            urls=[watch.url],
        )
        if not envelope["results"]:
            logger.warning("watch_check_produced_no_result", extra={"watch_id": watch.watch_id})
            return WatchOutcome.ERROR, watch.validators
        result = envelope["results"][0]
        if result.classification == CrawlClassification.not_modified:
            return WatchOutcome.NOT_MODIFIED, watch.validators
        if result.status != "success":
            return WatchOutcome.ERROR, watch.validators
        return WatchOutcome.MODIFIED, ContentValidators(
            etag=result.validators.get("etag"),
            last_modified=result.validators.get("last_modified"),
        )


async def sweep_watches_forever(
    store: WatchStore,
    *,
    host_policy: HostPolicy,
    settings: CrawlerSettings,
    shutdown_event: asyncio.Event,
    interval_seconds: float = WATCH_SWEEP_INTERVAL_SECONDS,
    clock: Callable[[], float] = time.time,
) -> None:
    """Sweep the registry until told to stop; the wait rides the shutdown event
    so a stopping process leaves at once instead of finishing an idle cadence.
    ``clock`` is a parameter for the loop's tests.
    """
    sweeper = WatchSweeper(
        store,
        host_policy=host_policy,
        settings=settings,
        shutdown_event=shutdown_event,
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
