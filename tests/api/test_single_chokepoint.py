"""Single-chokepoint law (#941): anonymous and key-verify share one store."""

from __future__ import annotations

from crawlutron_rest.api.throttle import ANON_MAX_REQUESTS, Throttle


def test_one_instance_serves_both_surfaces(tmp_path):
    """One Throttle serves both surfaces."""
    db = tmp_path / "crawler.db"
    meter = Throttle(shared_db_path=db)
    for _ in range(ANON_MAX_REQUESTS):
        meter.check_anon("203.0.113.1")
    meter.record_failure("ck_0123456789abcdef.fail")
    assert meter.shared is True
    assert meter.residence() == 2


def test_two_instances_over_one_db_agree(tmp_path):
    """M1: two instances bound to one DB see each other's buckets."""
    db = tmp_path / "crawler.db"
    first, second = Throttle(shared_db_path=db), Throttle(shared_db_path=db)
    first.check_anon("203.0.113.2")
    assert second.residence() == 1


async def test_admin_bucket_isolated_from_key_buckets(tmp_path):
    """The admin bucket never mixes with key-verify buckets."""
    db = tmp_path / "crawler.db"
    meter = Throttle(shared_db_path=db)
    for _ in range(5):
        await meter.arecord_failure("ck_0123456789abcdef.key")
    for _ in range(5):
        await meter.arecord_admin_failure()
    assert await meter.aresidence() == 2
