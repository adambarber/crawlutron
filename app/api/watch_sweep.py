"""The watch sweep: one pass rechecks every due watch and announces what changed.

A check is a conditional GET, not a crawl. The watch's stored validators go
out as ``If-None-Match`` / ``If-Modified-Since``, and only a delivered
representation counts as a change -- which is why the check runs the HTTP
strategy alone: rendering a page to answer "did this change?" would pay for a
body nobody asked for, and a ``304`` is answered before any body exists.

Every outcome decision is made here and handed to the store as a value. The
store holds rows and does not interpret results, so the two cannot disagree
about what a check meant. A check that reaches no verdict is an ``ERROR``:
it says nothing about whether the page changed, so it keeps the stored
validators, fires no webhook, and only extends the failure streak.
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
    """The single-URL policy a check runs under.

    HTTP alone, declared here rather than taken from the payload because the
    question a check answers is answered by a conditional GET. A chain that
    climbed to a browser would ask for a render to compare against a
    representation the caller never received, and would need the browser pool
    to answer a question the pool is not part of.
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
    """Rechecks due watches and announces what changed, one pass at a time.

    Collaborators are constructor arguments rather than module globals: the
    store is the deployment's, and the policy is the one in force now, so a
    watch registered under an allow-list that has since closed fails its check
    instead of quietly reaching a host this deployment no longer admits.
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

        One conclusion per due watch, in the order the registry hands them
        over: the caller reports progress from this, and the row itself is the
        record of what was stored.
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
        """One conditional check: the stored validators in, the verdict out.

        The second value is what the row should now hold. A ``304`` and a
        failure both keep what was already known -- neither told us anything
        new about the representation -- while a delivered body replaces the
        set outright, including dropping a validator the origin no longer
        sends.
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
                # The engine reads validators keyed by URL and reads each value
                # as the mapping it arrived as, so the declared model is dumped
                # rather than hand-spelled -- the keys stay the spellings the
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
    """Sweep the registry until the deployment is told to stop.

    The wait rides the shutdown event rather than a bare ``sleep``, so a
    stopping process leaves at once instead of finishing out a cadence it will
    never act on. ``clock`` is a parameter because the loop's own tests drive
    the passes they assert on; production reads the wall clock through the
    default.
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
            # One watch that makes the crawl raise must not end the sweep. The
            # alternative is a registry that quietly stops being checked --
            # every watch still reads ACTIVE, every status endpoint still
            # answers, and nothing anywhere reports a dead task.
            logger.exception("watch_sweep_pass_failed")
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=interval_seconds)
        except TimeoutError:
            continue
