"""M12: policy.headers may not carry chokepoint-bypassing header names.

Two families, both rejected at the *model* (rejection at the validation site,
never a silent strip at consumption, per #1912):

- Conditional headers (``if-none-match`` / ``if-modified-since``) belong to
  the ``validators`` map (M14).
- ``cookie`` / ``host`` / ``proxy-authorization`` / ``x-forwarded-*`` /
  ``x-real-ip`` bypass chokepoints or spoof caller identity to the origin:
  the cookie jar owns Cookie, the pinned-request chokepoint owns Host, and
  origin-side IP/identity headers are never a crawl input.

``authorization`` is deliberately NOT banned here (user ruling 2026-09-22):
credentialed crawling of one's own site is a real use case, and banning the
header without a typed replacement would push callers into worse patterns
(baked-in URL credentials). It moves under the ``policy.credentials``
chokepoint when that lands (Tier 2b); until then the ban list and this test
pin exactly the six, no more.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

BANNED = (
    "if-none-match",
    "if-modified-since",
    "cookie",
    "host",
    "proxy-authorization",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-real-ip",
)


@pytest.mark.parametrize("header", BANNED)
@pytest.mark.parametrize("spelling", ["Cookie", "COOKIE", "cookie ", "cookie"])
def test_banned_headers_rejected_in_any_spelling(header, spelling):
    from app.api_models import CrawlRequest

    with pytest.raises(ValidationError):
        CrawlRequest(url="http://example.com/", policy={"headers": {spelling: "x"}})


@pytest.mark.parametrize("model_name", ["CrawlRequest", "MapRequest", "JobCreateRequest"])
def test_the_ban_travels_with_the_shared_policy(model_name):
    """Every model embedding CrawlPolicy inherits the ban from one site.

    The three crawl-shaped request models (crawl, map, jobs) all embed the
    same CrawlPolicy, so anchoring the validator there covers them at once.
    ``RenderRequest`` embeds ``RenderOptionsRequest``, which has no ``headers``
    field -- it is structurally immune and correctly excluded here.
    """
    from app import api_models

    model = getattr(api_models, model_name)
    with pytest.raises(ValidationError):
        model(url="http://example.com/", policy={"headers": {"X-Forwarded-For": "1.2.3.4"}})


def test_render_request_has_no_header_smuggling_surface():
    """Pin the structural immunity that keeps RenderRequest out of the ban list.

    If a ``headers`` field is ever added to RenderOptionsRequest, this fails
    and forces the ban to cover it -- immunity must be re-earned, not assumed.
    """
    from app.api_models import RenderOptionsRequest

    assert "headers" not in RenderOptionsRequest.model_fields


def test_authorization_stays_allowed_until_the_credentials_field_lands():
    """Pin the ruling, not an accident.

    If a future change bans ``authorization`` here, it must do it *together
    with* ``policy.credentials`` so the typed path exists first; deleting a
    caller's only credentialed-crawl option without a replacement is the
    workaround-forcing move this test exists to catch in review.
    """
    from app.api_models import CrawlRequest

    request = CrawlRequest(url="http://example.com/", policy={"headers": {"Authorization": "Bearer t"}})
    assert request.policy.headers == {"Authorization": "Bearer t"}


def test_benign_headers_are_unaffected():
    from app.api_models import CrawlRequest

    headers = {
        "Accept": "text/html",
        "User-Agent": "my-crawler/1.0",
        "Accept-Language": "en",
        "From": "ops@example.com",
    }
    request = CrawlRequest(url="http://example.com/", policy={"headers": headers})
    assert request.policy.headers == headers


def test_conditional_headers_keep_their_specific_reason():
    """The M14 reason survives the consolidation: conditionals point at validators."""
    from app.api_models import CrawlRequest

    with pytest.raises(ValidationError, match="validators"):
        CrawlRequest(url="http://example.com/", policy={"headers": {"If-None-Match": '"v1"'}})
