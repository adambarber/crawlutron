"""Loop-lag probe scripting, re-exported from the one clock home (#843).

The scripted clock/sleep bodies moved to ``tests/_clocks.py``; the module
stays so ``tests.test_loop_lag_probe`` and the gauge suite keep their path.
"""

from tests._clocks import lag_clock, lagged_gate, scripted_sleep

__all__ = ["lag_clock", "lagged_gate", "scripted_sleep"]
