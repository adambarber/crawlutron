"""The webhook admission chokepoint: one order, validate then admit.

All three legs live in ``admit_webhook_target``; every door calls this single
façade so a door that forgets a leg is structurally impossible.
"""

import asyncio
import time

import pytest
from safe_url.host_policy import HostPolicy

from crawlutron.models.caller_identity import CallerIdentity
from crawlutron.service.caller_auth import Tracking, admit
from crawlutron.service.caller_concurrency import CallerConcurrencyTracker

# The admission law plus the chokepoint facade the two routers share (#1082).
from crawlutron.service.errors import QuotaRefusal
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


async def test_slow_webhook_probe_outside_admit_does_not_tax_concurrent_admit():
    """A slow webhook probe must not tax a concurrent admit for the same caller.

    Moving the probe back inside ``admission.admit`` reds this test.
    """
    tracker = CallerConcurrencyTracker()
    caller = CallerIdentity(caller_id="agent-a", authenticated=True, max_pages=100, max_concurrency=1)
    probe_started = asyncio.Event()

    # Request 1: slow probe runs FIRST, then admit takes the reservation (fixed order).
    async def request_one():
        probe_started.set()
        await asyncio.sleep(2.0)  # slow probe; no reservation held yet
        async with admit(
            caller, 10, tracking=Tracking(job_store=None, concurrency_tracker=tracker), payload=None
        ):
            pass

    task1 = asyncio.create_task(request_one())
    await asyncio.wait_for(probe_started.wait(), timeout=5)

    # Request 2: same caller, no probe -- must not be taxed by request 1's probe.
    started = time.monotonic()
    admitted = False
    try:
        async with admit(
            caller, 10, tracking=Tracking(job_store=None, concurrency_tracker=tracker), payload=None
        ):
            admitted = True
    except QuotaRefusal:
        admitted = False
    elapsed = time.monotonic() - started

    assert admitted is True, "concurrent admit was refused while request 1's probe was in flight"
    assert elapsed < 1.0, f"concurrent admit waited {elapsed:.2f}s for the slow webhook probe"

    await asyncio.wait_for(task1, timeout=6)
