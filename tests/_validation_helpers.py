from crawlutron.config.env import get_env_int
from crawlutron.models.normalized_url import NormalizedUrl
from crawlutron.models.normalized_url import admit_or_none as _admit_or_none
from crawlutron.models.registrable_domain import registrable_domain
from tests._policy_factories import UNRESTRICTED_HOST_POLICY


def admit_or_none(candidate, *, policy=UNRESTRICTED_HOST_POLICY):
    """Production admission with the test default policy pre-bound."""
    return _admit_or_none(candidate, policy=policy)


def assert_url_refused(url: str, policy, reason: str) -> None:
    """``NormalizedUrl`` admission must reject `url` for exactly `reason`; stated as
    a bare ``pytest.raises(ValueError)`` the reason goes missing, and which reason
    wins is the entire subject of the host-policy tests.
    """
    import pytest

    with pytest.raises(ValueError, match=reason):
        NormalizedUrl(url, policy=policy)


__all__ = [
    "UNRESTRICTED_HOST_POLICY",
    "admit_or_none",
    "assert_url_refused",
    "get_env_int",
    "registrable_domain",
]
