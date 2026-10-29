"""Delayed-host pacing keys the batch on the canonical site (#497)."""

from crawlutron.admission.admission import JobAdmissionContext
from crawlutron.admission.policy import PolicyTier
from crawlutron.api.job_runner import pace_delayed_batch
from crawlutron.config.settings import CrawlerSettings
from crawlutron.models.crawl_policy import CrawlPolicy
from crawlutron.storage.job_store import FrontierEntry
from tests._policy_factories import canonical


def _context() -> JobAdmissionContext:
    return JobAdmissionContext.create(
        CrawlPolicy(), PolicyTier.model_defaults(), CrawlerSettings.from_mapping({})
    )


async def test_subdomain_entry_is_paced_when_site_delay_was_committed(monkeypatch):
    """A www.x.com entry must match a delay committed for the site x.com."""
    context = _context()
    context.commit_crawl_delay("x.com", 0.1)

    entry = FrontierEntry(url=canonical("http://www.x.com/page"), depth=0)
    ran: list[str] = []

    async def fake_run(tasks, worker, failed_result=None):
        ran.extend(task.normalized_url.raw for task in tasks)
        return []

    monkeypatch.setattr(context.scheduler, "run", fake_run)
    await pace_delayed_batch(context, [entry])
    assert ran == [entry.url.raw], "www.x.com must match a delay committed for x.com"
