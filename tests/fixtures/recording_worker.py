"""The ``recording_worker`` fixture: a recursive-crawl worker that records jobs."""

import pytest


@pytest.fixture
def recording_worker():
    """``(seen, worker)``: a recursive-crawl worker that records its job URLs.

    Several dispatch and robots tests assert *which* jobs reached the worker;
    a copy that drops the ``seen.append`` turns that into a vacuous truth."""
    from crawlutron.models.normalized_url import NormalizedUrl
    from tests._result_builders import make_engine_result

    seen: list[NormalizedUrl] = []

    async def worker(job):
        seen.append(job.normalized_url)
        return make_engine_result(job.normalized_url)

    return seen, worker
