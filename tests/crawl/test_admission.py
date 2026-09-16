"""Resource-pressure admission and readiness gating."""

from app.health.readiness import ReadinessState
from app.metrics import get_metrics


class TestReadinessState:
    def test_initial_state_is_ready(self):
        state = ReadinessState()
        snap = state.snapshot()
        assert snap["status"] == "ready"
        assert snap["active_requests"] == 0
        assert snap["saturated"] is False

    def test_begin_end_request(self):
        state = ReadinessState()
        state.begin_request()
        snap = state.snapshot()
        assert snap["active_requests"] == 1
        assert snap["saturated"] is False
        state.end_request()
        snap = state.snapshot()
        assert snap["active_requests"] == 0

    def test_saturated_when_memory_at_ceiling(self):
        metrics = get_metrics()
        metrics.resource_memory_utilization = 0.0
        metrics.resource_cpu_utilization = 0.0
        state = ReadinessState()
        assert state.snapshot()["saturated"] is False
        # Memory gate rides at 0.92; at/above target the service is saturated.
        metrics.resource_memory_utilization = 0.95
        snap = state.snapshot()
        assert snap["saturated"] is True
        assert snap["status"] == "saturated"

    def test_saturated_when_cpu_at_ceiling(self):
        metrics = get_metrics()
        metrics.resource_memory_utilization = 0.0
        metrics.resource_cpu_utilization = 0.0
        state = ReadinessState()
        # CPU gate rides at 0.90; at/above target the service is saturated.
        metrics.resource_cpu_utilization = 0.93
        assert state.snapshot()["saturated"] is True

    def test_end_request_does_not_go_negative(self):
        state = ReadinessState()
        state.end_request()
        snap = state.snapshot()
        assert snap["active_requests"] == 0
