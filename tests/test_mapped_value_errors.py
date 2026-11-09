"""The constructor belt's foreign-ValueError mapping is recorded, not silent."""

import pytest

import crawlutron.models.normalized_url as nu
from crawlutron.models.normalized_url import NormalizedUrl
from crawlutron.runtime.resource_lifecycle import clear_mapped_value_errors, mapped_value_errors
from tests._policy_factories import UNRESTRICTED_HOST_POLICY


def _foreign(host: str) -> str:
    raise ValueError("foreign programming bug")


def test_a_foreign_value_error_is_mapped_and_recorded() -> None:
    original = nu.idna_host
    nu.idna_host = _foreign
    clear_mapped_value_errors()
    try:
        with pytest.raises(ValueError, match="invalid_url"):
            NormalizedUrl("http://example.com/", policy=UNRESTRICTED_HOST_POLICY)
        assert mapped_value_errors() == (("foreign programming bug",),)
    finally:
        nu.idna_host = original
        clear_mapped_value_errors()


def test_a_vocabulary_value_error_is_not_recorded() -> None:
    clear_mapped_value_errors()
    with pytest.raises(ValueError, match="userinfo_refused"):
        NormalizedUrl("http://user@example.com/", policy=UNRESTRICTED_HOST_POLICY)
    assert mapped_value_errors() == ()
