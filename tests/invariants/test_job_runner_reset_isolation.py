"""#1424: job_runner's install_execute_crawl state is cleared by process_state reset.

Without registration, install_execute_crawl's rebinding leaks across test order.
"""

from __future__ import annotations

from crawlutron.models.result_record import ProducerEnvelope
from crawlutron.runtime import process_state
from crawlutron.service import job_runner


async def _fake_execute_crawl(*args: object, **kwargs: object) -> ProducerEnvelope:
    """Awaitable fake satisfying the ExecuteCrawl type; never actually invoked."""
    return ProducerEnvelope(
        request_id="test",
        submitted_count=0,
        policy_schema_version="1",
        results=[],
    )


def test_job_runner_state_cleared_by_process_state_reset() -> None:
    """Installing a fake execute_crawl then resetting all process state leaves
    job_runner with no residual state, regardless of test order.
    """
    job_runner.install_execute_crawl(_fake_execute_crawl)
    try:
        process_state.reset_all_process_state()
        has_state = process_state.owner_has_state("crawlutron.service.job_runner")
    except KeyError:
        # Not registered at all: the reset family never sees this owner's state.
        has_state = True
    finally:
        job_runner.reset_execute_crawl()
    assert not has_state
