"""Hostful identity: an admitted URL carries a real, re-admittable host."""

from __future__ import annotations

import pytest

import safe_url.normalized_url as normalized_url
from safe_url.host_canonical import idna_host, punycode_host
from safe_url.host_policy import HostPolicy, host_policy_reason
from safe_url.normalized_url import NormalizedUrl
from safe_url.registrable_domain import registrable_domain

UNRESTRICTED = HostPolicy.unrestricted()


@pytest.mark.parametrize("host", [".", "..", "...", ""])
def test_idna_host_refuses_dot_only_or_empty(host: str) -> None:
    """The encoder's empty result never becomes an admitted host."""
    with pytest.raises(ValueError, match="invalid_idn_host"):
        idna_host(host)


@pytest.mark.parametrize("raw", ["http://./", "http://../", "http://.../"])
def test_dot_only_authority_is_refused(raw: str) -> None:
    """A host that transforms to nothing cannot cross admission."""
    with pytest.raises(ValueError):
        NormalizedUrl(raw, policy=UNRESTRICTED)


def test_empty_transformed_host_is_refused_before_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """The constructor guard holds even if the encoder stops refusing empties."""
    monkeypatch.setattr(normalized_url, "idna_host", lambda _host: "")
    with pytest.raises(ValueError, match="invalid_url"):
        NormalizedUrl("http://./", policy=UNRESTRICTED)


def test_leading_dot_host_is_refused() -> None:
    """An empty first label is refused by the IDNA door."""
    with pytest.raises(ValueError, match="invalid_idn_host"):
        NormalizedUrl("http://.example.com", policy=UNRESTRICTED)


def test_trailing_dots_canonicalize_to_the_real_host() -> None:
    """Trailing FQDN dots are stripped, leaving a host that re-admits."""
    url = NormalizedUrl("http://example.com..", policy=UNRESTRICTED)
    assert url.host == "example.com"
    assert url.raw == "http://example.com"


def test_policy_matching_keeps_passthrough() -> None:
    """Policy entries and site matching do not harden with the admission door."""
    assert host_policy_reason("", policy=UNRESTRICTED) is None
    assert HostPolicy.from_strings(".", "").allowed_hosts == ()
    assert punycode_host("") == ""
    assert registrable_domain(".") == ""
