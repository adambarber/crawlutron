"""The declared reads on the own-write storage doors (#136).

Each locks a checked read: the reader validates the shape it wrote rather than
trusting ``json.loads``'s ``Any`` -- a corrupt record is a miss, never a 500.
"""

import json
from collections.abc import Callable

from crawlutron.admission.policy import read_policy
from crawlutron.transport.content_cache import ContentCache
from tests._job_drivers import raw_job_row
from tests._job_harnesses import make_job_store


async def test_a_non_integer_stats_column_reads_as_its_declared_zeros(tmp_path):
    """The stats column is written from ``dict[str, int]``; the read is checked,
    not ``json.loads``'s raw value poured into a view that declares ``int``."""
    store = make_job_store(tmp_path)
    job = store.create("https://example.test/root", read_policy({"limits": {"max_depth": 2}}))
    with raw_job_row(store) as connection:
        connection.execute(
            "UPDATE crawl_jobs SET stats = ? WHERE job_id = ?",
            ('{"total_crawled": {"deep": 1}, "total_discovered": 7}', job.job_id),
        )

    status = await store.astatus(job.job_id)

    assert status is not None
    assert status["stats"] == {
        "total_crawled": 0,
        "total_failed": 0,
        "cap_truncated": 0,
        "cap_overcommitted": 0,
        "total_discovered": 7,
    }, "an undeclared stats read served a non-int value the view declares int"


def test_a_frontier_entry_that_is_not_an_entry_fails_at_persist(tmp_path):
    """The write edge serializes ``FrontierEntry`` records only: a bare string in
    the frontier is a writer bug and dies at persist, not in the reader."""
    store = make_job_store(tmp_path)
    job = store.create("https://example.test/root", read_policy({"limits": {"max_depth": 2}}))
    # An APPEND is the delta the write edge serializes: the bare string must die
    # there, never land as a row. The append itself is the deliberate violation.
    bad_append: Callable[..., object] = job.frontier.append
    bad_append("https://example.test/bare")
    try:
        store.save(job)
    except (AttributeError, TypeError):
        pass
    else:
        raise AssertionError("a non-entry frontier element was persisted silently")


async def test_a_valid_json_non_envelope_cache_file_is_a_miss_not_a_crash(tmp_path):
    """A cached file holding a JSON array parses as valid JSON but is not the
    declared envelope; the checked read answers None instead of raising out."""
    cache = ContentCache(tmp_path)
    await cache.aset("key", {"body": "ok"})
    entry = next(tmp_path.glob("*.json"))
    entry.write_bytes(json.dumps([1, 2]).encode())
    cold = ContentCache(tmp_path)  # no RAM mirror: the corrupted file is the only copy
    assert await cold.aget("key") is None, "a non-envelope file must read as a miss"
