"""#640: the sweep's log sinks emit the redacted projection, never the raw URL. The watch_sweep call sites
are the category's live surface: every ``url``-keyed value a watch_sweep record carries must be a
WatchState redaction, so a raw credentialed URL cannot reemerge in a sink without this failing.
"""

from __future__ import annotations

import logging

from crawlutron.models.host_policy import HostPolicy
from crawlutron.models.normalized_url import NormalizedUrl
from tests._policy_factories import canonical
from tests._watch_harnesses import (
    UNRESTRICTED_HOST_POLICY,
    CrawlClassification,
    CrawlerSettings,
    WatchSweeper,
    WebhookTarget,
    default_sweeper,
    loopback_url,
    watch_sweep,
)

NOW = 1_700_000_000.0
INTERVAL = 3600
SECRET_URL = "https://watcher.example.com/page?key=secret"
SECRET_WEBHOOK = "https://watcher.example.com/hook?key=secret"


async def _raise(*args, **kwargs):
    raise RuntimeError("the crawl raised")


def _assert_no_secret_in_any_record(caplog):
    for record in caplog.records:
        assert "secret" not in str(record.__dict__), record.getMessage()


async def test_the_check_failure_sink_emits_the_redacted_url(caplog, watch_store, monkeypatch):
    """``watch_check_failed`` logs the redacted projection, never ``watch.url``."""
    watch_store.create(canonical(SECRET_URL), interval_seconds=INTERVAL, now=NOW)
    monkeypatch.setattr(watch_sweep, "execute_crawl", _raise)
    with caplog.at_level(logging.WARNING, logger="crawlutron.service.watch_sweep"):
        outcomes = await default_sweeper(watch_store).sweep_once(now=NOW + INTERVAL)

    assert outcomes == [CrawlClassification.unknown_error]
    failed = [r for r in caplog.records if r.getMessage() == "watch_check_failed"]
    assert failed, caplog.records
    assert "secret" not in failed[0].__dict__["url"], failed[0].__dict__
    _assert_no_secret_in_any_record(caplog)


async def test_the_webhook_admission_failure_sink_emits_the_redacted_url(caplog, watch_store, etag_server):
    """``watch_webhook_admission_failed`` logs the redacted webhook, never the raw one. The watch checks
    against a live origin; the webhook's host is denied by the live policy, so ``watch.target()``
    re-admission refuses exactly as production does.
    """
    # Built past admission (a secret-named query param is refused at the door
    # now); this pins the sweep's re-admission refusal log redaction behind it.
    target = WebhookTarget(target=NormalizedUrl(SECRET_WEBHOOK, policy=UNRESTRICTED_HOST_POLICY))
    watch_store.create(
        canonical(loopback_url(etag_server)),
        webhook_url=target,
        interval_seconds=INTERVAL,
        now=NOW,
    )

    sweeper = WatchSweeper(
        watch_store,
        host_policy=HostPolicy.from_strings("", "watcher.example.com"),
        settings=CrawlerSettings.from_mapping(),
    )
    with caplog.at_level(logging.WARNING, logger="crawlutron.service.watch_sweep"):
        outcomes = await sweeper.sweep_once(now=NOW + INTERVAL)

    # The check succeeded (rendered); the webhook refusal is logged, the change
    # announced to nobody, and the pinned sink record carries only the redaction.
    assert outcomes == [CrawlClassification.rendered]
    admitted = [r for r in caplog.records if r.getMessage() == "watch_webhook_admission_failed"]
    assert admitted, caplog.records
    assert "secret" not in admitted[0].__dict__["url"], admitted[0].__dict__
    _assert_no_secret_in_any_record(caplog)
