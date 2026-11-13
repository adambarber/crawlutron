"""Whitespace-bearing hosts are structurally refused, not admitted."""

import pytest
from safe_url.normalized_url import NormalizedUrl

from tests._policy_factories import UNRESTRICTED_HOST_POLICY


@pytest.mark.parametrize(
    "raw",
    [
        "http://exa mple.com/",  # internal space in the host
        "http://exa\tmple.com/",  # tab folds silently today
        "http://exa\nmple.com/",  # newline folds silently today
        "http:// example.com/",  # leading space in the host
        "http://example.com /",  # trailing space in the host
        "http://exa mple.com:8080/",  # space in the host with a port
    ],
)
def test_a_whitespace_host_is_refused_as_invalid_url(raw: str) -> None:
    with pytest.raises(ValueError, match="invalid_url"):
        NormalizedUrl(raw, policy=UNRESTRICTED_HOST_POLICY)


def test_space_in_the_path_is_not_a_host_refusal() -> None:
    # The whitespace law is about the authority segment: a space in a path/query
    # is identity text, and the canonicalization history for it is unchanged.
    url = NormalizedUrl("http://example.com/a b", policy=UNRESTRICTED_HOST_POLICY)
    assert url.raw == "http://example.com/a b"
