"""The ``four_worker_selector_pool`` fixture: the #521 semantics at a fixed size."""

import pytest


@pytest.fixture
def four_worker_selector_pool(monkeypatch):
    """Pin the selector pool at 4 via the documented knob and rebuild it.

    The #521 cancel/shutdown semantics are pool-size-independent; a small fixed
    pool keeps their saturation arithmetic deterministic.
    """
    from tests._extraction_helpers import pin_selector_pool

    pin_selector_pool(monkeypatch)
