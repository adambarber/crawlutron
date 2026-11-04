"""DomainState.liveness: the gate question is answered from public state.

``live()`` used to read ``asyncio.Semaphore._value``, a CPython implementation
detail; the occupancy question is now explicit on the record (#314/A3).
"""

from dataclasses import dataclass

from crawlutron.transport.scheduler_domain_state import DomainState


@dataclass
class _Gate:
    """A gate-like object with no ``_value``: only the public contract exists."""

    occupancy: int = 0


def _state(gate_occupancy: int = 0) -> DomainState:
    state = DomainState()
    state.gate = _Gate(gate_occupancy)
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
