"""The webhook admission chokepoint: one order, validate then admit.

All three legs live in ``admit_webhook_target``; every door calls this single
façade so a door that forgets a leg is structurally impossible.
"""

import pytest
from safe_url.host_policy import HostPolicy

from crawlutron.service.webhooks import admit_webhook_target
from tests._assertions import host_resolves_to


async def test_admit_refuses_non_https_scheme():
    """Leg 1: a non-https callback is refused before any DNS or admission work."""
    with pytest.raises(ValueError, match="https://"):
        await admit_webhook_target("http://receiver.example/hook", host_policy=HostPolicy.unrestricted())


async def test_admit_refuses_private_resolved_target(monkeypatch):
    """Leg 2: the pinned-request SSRF rule refuses a private/resolved target."""
    with host_resolves_to(monkeypatch, "127.0.0.1"), pytest.raises(ValueError, match="SSRF"):
        await admit_webhook_target("https://evil.example.com/callback", host_policy=HostPolicy.unrestricted())


async def test_admit_refuses_userinfo(monkeypatch):
    """Leg 3: admission refuses userinfo in the URL (NormalizedUrl's law)."""
    with host_resolves_to(monkeypatch, "8.8.8.8"), pytest.raises(ValueError, match="userinfo"):
        await admit_webhook_target(
            "https://user:pass@example.com/hook", host_policy=HostPolicy.unrestricted()
        )


async def test_admit_admits_https_public_target(monkeypatch):
    """A https public target passes all three legs and returns the admitted target."""
    with host_resolves_to(monkeypatch, "8.8.8.8"):
        target = await admit_webhook_target(
            "https://webhook.example.com/cb", host_policy=HostPolicy.unrestricted()
        )
    assert target.delivery_url() == "https://webhook.example.com/cb"
