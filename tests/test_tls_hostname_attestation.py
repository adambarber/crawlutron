"""TLS attestation names the canonical host, never an IP-literal origin."""

from safe_url.normalized_url import NormalizedUrl
from tests._policy_factories import UNRESTRICTED_HOST_POLICY


def test_https_ip_literal_has_no_tls_hostname():
    url = NormalizedUrl("https://8.8.8.8/x", policy=UNRESTRICTED_HOST_POLICY)
    assert url.tls_hostname is None


def test_https_hostname_is_the_tls_hostname():
    url = NormalizedUrl("https://example.com/x", policy=UNRESTRICTED_HOST_POLICY)
    assert url.tls_hostname == "example.com"
