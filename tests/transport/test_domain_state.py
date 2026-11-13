"""DomainState.liveness: the gate question is answered from public state.

``live()`` used to read ``asyncio.Semaphore._value``, a CPython implementation
detail; the occupancy question is now explicit on the record (#314/A3).
"""

from crawlutron.transport.scheduler_domain_state import DomainState


def _state(gate_occupancy: int = 0) -> DomainState:
    """A record whose gate holds ``gate_occupancy`` in-flight holders: the real
    DomainGate with occupancy set, so the public contract (not ``_value``) is
    the thing under test."""
    state = DomainState()
    state.gate.occupancy = gate_occupancy
    return state


class TestGateLiveness:
    """A holder or waiter keeps the record; an idle gate does not."""

    def test_a_half_full_gate_keeps_the_record_live(self):
        """#577 F1: limit 2, one holder -- ``locked()`` alone said dead and a
        purge would have re-minted a FULL fresh gate under the old holder."""
        assert _state(gate_occupancy=1).live(now=10.0, window=5.0, pin_ttl=5.0)

    def test_an_idle_gate_does_not_keep_the_record_live(self):
        assert not _state(gate_occupancy=0).live(now=10.0, window=5.0, pin_ttl=5.0)

    def test_a_gate_at_capacity_keeps_the_record_live(self):
        assert _state(gate_occupancy=2).live(now=10.0, window=5.0, pin_ttl=5.0)

    def test_the_gate_question_answers_without_a_semaphore(self):
        """The defect: ``live()`` raised AttributeError on any gate lacking the
        private ``_value`` counter; the public occupancy is the only source."""
        assert _state(gate_occupancy=1).gate.occupancy == 1


class TestCrawlDelayLiveness:
    """A robots Crawl-delay floor does not keep a record live by itself:
    only recent activity (ema/last_seen within window) does."""

    def _record(self, *, min_delay: float, ema: float | None, last_seen: float | None) -> DomainState:
        state = DomainState()
        state.min_delay = min_delay
        state.ema = ema
        state.last_seen = last_seen
        return state

    def test_domain_with_only_crawl_delay_and_stale_activity_is_dead(self):
        """min_delay > 0 is not liveness: a floor-only record with a stale
        last_seen is purgeable -- the old short-circuit kept it forever."""
        state = self._record(min_delay=5.0, ema=100.0, last_seen=0.0)
        assert not state.live(now=1000.0, window=600.0, pin_ttl=300.0)

    def test_domain_with_crawl_delay_and_recent_activity_stays_live(self):
        """min_delay plus a fresh last_seen/ema is live: liveness now comes
        from the age check, not the floor alone."""
        state = DomainState()
        state.min_delay = 5.0
        state.feed_latency(100.0, smoothing=0.2, clock_now=990.0)
        assert state.live(now=1000.0, window=600.0, pin_ttl=300.0)

    def test_domain_with_only_crawl_delay_within_window_stays_live(self):
        """A floor-only record whose last_seen is inside the window is live
        through the age check."""
        state = self._record(min_delay=5.0, ema=100.0, last_seen=990.0)
        assert state.live(now=1000.0, window=600.0, pin_ttl=300.0)

    def test_domain_at_the_window_edge_is_not_live(self):
        """last_seen == now - window is already dead: the check is strict."""
        state = self._record(min_delay=5.0, ema=100.0, last_seen=400.0)
        assert not state.live(now=1000.0, window=600.0, pin_ttl=300.0)

    def test_domain_just_inside_the_window_stays_live(self):
        state = self._record(min_delay=5.0, ema=100.0, last_seen=400.0 + 1e-6)
        assert state.live(now=1000.0, window=600.0, pin_ttl=300.0)
