"""Cross-worker throttle sharing (#941): independent buckets must not multiply limits."""

from __future__ import annotations

import pytest

from crawlutron.service.errors import KeyVerifyThrottled
from crawlutron_rest.api.throttle import (
    ANON_MAX_REQUESTS,
    Throttle,
    Throttled,
)


def test_anonymous_budget_shared_across_worker_instances(tmp_path):
    """Two worker-local meters bound to the same DB share one IP bucket."""
    db = tmp_path / "crawler.db"
    a = Throttle(shared_db_path=db)
    b = Throttle(shared_db_path=db)
    remote = "203.0.113.7"
    for _ in range(ANON_MAX_REQUESTS):
        a.check_anon(remote)
    with pytest.raises(Throttled):
        b.check_anon(remote)


def test_key_verify_failures_shared_across_worker_instances(tmp_path):
    """Failure ladders aggregate across workers on the same shared DB."""
    db = tmp_path / "crawler.db"
    a = Throttle(shared_db_path=db)
    b = Throttle(shared_db_path=db)
    secret = "ck_0123456789abcdef.shared"
    for _ in range(Throttle.MAX_FAILURES):
        a.record_failure(secret)
    with pytest.raises(KeyVerifyThrottled):
        b.check(secret)
