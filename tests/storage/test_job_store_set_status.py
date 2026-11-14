"""Job store set_status claim/clear semantics."""

import pytest

from crawlutron.models.crawl_policy import CrawlPolicy
from crawlutron.storage.job_store import JobStatus


def test_set_status_rejects_clear_claim_with_stamp_args(job_store):
    """clear_claim cannot be combined with claim stamping args."""
    job = job_store.create("http://example.com/", CrawlPolicy())
    job_store.set_status(job.job_id, JobStatus.RUNNING, expected=JobStatus.PENDING)
    assert job_store.claim_running(job.job_id, "owner", now=1000.0, claim_seconds=300.0)
    with pytest.raises(ValueError, match="clear_claim conflicts with claim_now"):
        job_store.set_status(
            job.job_id,
            JobStatus.FAILED,
            expected=JobStatus.RUNNING,
            claimed_by="owner",
            claim_now=1001.0,
            clear_claim=True,
        )
