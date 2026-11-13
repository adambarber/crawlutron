import pytest
from pydantic import ValidationError
from safe_url.normalized_url import NormalizedUrl
from safe_url.normalized_url import admit_or_none as _admit_or_none
from safe_url.registrable_domain import registrable_domain

from crawlutron.config.env import get_env_int
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


def assert_non_finite_float_field_refused(make_request, field: str) -> bool:
    """``make_request(**{field: value})`` must refuse nan/inf/-inf at validation.

    Non-finite floats ride Python's permissive JSON parser onto fields whose
    comparisons they silently corrupt (e.g. NaN never compares greater-than).
    """
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValidationError):
            make_request(**{field: bad})
    return True


__all__ = [
    "UNRESTRICTED_HOST_POLICY",
    "admit_or_none",
    "assert_non_finite_float_field_refused",
    "assert_url_refused",
    "get_env_int",
    "registrable_domain",
]
