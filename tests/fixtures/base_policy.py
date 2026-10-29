"""The ``base_policy`` fixture: a fresh minimal crawl policy."""

import pytest


@pytest.fixture
def base_policy():
    """Fresh minimal crawl policy; tests may mutate their copy freely."""
    from tests._policy_factories import make_base_policy

    return make_base_policy()
