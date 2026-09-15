"""T8: the cooldown purge must not rebuild both scheduler dicts per 429.

Every rate-limited result calls ``set_domain_cooldown``, which used to end with
``_purge_expired()`` — two full dict comprehensions per call, so a crawl with
many 429s across many domains paid quadratic work. The purge is amortized to a
bounded interval (``run()`` still purges up front).
"""

import pytest

from app.scheduler import DomainScheduler


class TestCooldownPurgeCadence:
    def test_purge_is_amortized_not_per_rate_limited_result(self, monkeypatch):
        calls = {"n": 0}
        real_purge = DomainScheduler._purge_expired

        def counting_purge(self):
            calls["n"] += 1
            real_purge(self)

        monkeypatch.setattr(DomainScheduler, "_purge_expired", counting_purge)
        scheduler = DomainScheduler(global_limit=1, per_domain_limit=1, delay_seconds=0.0)
        for i in range(250):
            scheduler.set_domain_cooldown(f"d{i}.test", 5.0)
        # 250 sets on a 100-set interval: the cadence is amortized, not 1:1.
        assert calls["n"] <= 3

    def test_expired_cooldowns_are_still_reclaimed(self, monkeypatch):
        """Amortizing must not let expired entries accumulate indefinitely:
        once the interval elapses only expired entries drop, and the fresh
        cooldown for the setting domain survives."""
        fake_now = [1000.0]
        monkeypatch.setattr("app.scheduler.time.time", lambda: fake_now[0])
        scheduler = DomainScheduler(global_limit=1, per_domain_limit=1, delay_seconds=0.0)
        scheduler.set_domain_cooldown("old.test", 10.0)
        fake_now[0] = 1100.0  # the first cooldown has elapsed
        # Burn past the interval so the amortized purge fires.
        for i in range(101):
            scheduler.set_domain_cooldown(f"d{i}.test", 5.0)
        assert "old.test" not in scheduler._domain_cooldowns
        assert scheduler._domain_cooldowns, "fresh cooldowns were dropped"

    def test_a_fresh_cooldown_is_never_purged_by_the_amortized_run(self, monkeypatch):
        """The domain that just earned a cooldown is never removed by the purge
        that follows its own set."""
        monkeypatch.setattr("app.scheduler.time.time", lambda: 1000.0)
        scheduler = DomainScheduler(global_limit=1, per_domain_limit=1, delay_seconds=0.0)
        scheduler.set_domain_cooldown("rate-limited.test", 30.0)
        assert "rate-limited.test" in scheduler._domain_cooldowns
        assert scheduler._domain_cooldowns["rate-limited.test"] > 1000.0


@pytest.mark.parametrize("method", ["run"])
def test_run_still_purges_up_front(method):
    """The batch entry point keeps its up-front purge so a long run cannot grow
    the dicts without bound even when no 429 fires."""
    scheduler = DomainScheduler(global_limit=1, per_domain_limit=1, delay_seconds=0.0)
    scheduler._domain_cooldowns["stale.test"] = 0.0  # already expired
    assert "stale.test" in scheduler._domain_cooldowns
    # The up-front purge fires before any job runs.
    scheduler._purge_expired()
    assert "stale.test" not in scheduler._domain_cooldowns
