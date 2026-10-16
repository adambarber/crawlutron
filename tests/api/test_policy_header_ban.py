"""Policy.headers may not carry chokepoint-bypassing header names.

Conditional headers belong to ``validators``; cookie/host/x-real-ip bypass
chokepoints. ``authorization`` stays (ruling 2026-09-22) until credentials."""

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
    from app.models.crawl_request import CrawlRequest

    with pytest.raises(ValidationError):
        CrawlRequest(url="http://example.com/", policy={"headers": {spelling: "x"}})


@pytest.mark.parametrize(
    ("module_name", "model_name"),
    [
        ("app.models.crawl_request", "CrawlRequest"),
        ("app.models.map_request", "MapRequest"),
        ("app.models.job_create_request", "JobCreateRequest"),
    ],
)
def test_the_ban_travels_with_the_shared_policy(module_name, model_name):
    """Every model embedding CrawlPolicy inherits the ban from one site.

    The three crawl-shaped request models embed the same CrawlPolicy, so the
    validator covers them at once; ``RenderRequest`` has no ``headers`` field."""
    import importlib

    model = getattr(importlib.import_module(module_name), model_name)
    with pytest.raises(ValidationError):
        model(url="http://example.com/", policy={"headers": {"X-Forwarded-For": "1.2.3.4"}})


def test_render_request_has_no_header_smuggling_surface():
    """Pin the structural immunity that keeps RenderRequest out of the ban list.

    If a ``headers`` field is ever added to RenderOptionsRequest, this fails
    and forces the ban to cover it -- immunity must be re-earned, not assumed.
    """
    from app.models.render_options_request import RenderOptionsRequest

    assert "headers" not in RenderOptionsRequest.model_fields


def test_authorization_stays_allowed_until_the_credentials_field_lands():
    """Pin the ruling, not an accident.

    A future ban of ``authorization`` must land WITH ``policy.credentials``
    first; deleting the only credentialed-crawl option without one is caught."""
    from app.models.crawl_request import CrawlRequest

    request = CrawlRequest(url="http://example.com/", policy={"headers": {"Authorization": "Bearer t"}})
    assert request.policy.headers == {"Authorization": "Bearer t"}


def test_benign_headers_are_unaffected():
    from app.models.crawl_request import CrawlRequest

    headers = {
        "Accept": "text/html",
        "User-Agent": "my-crawler/1.0",
        "Accept-Language": "en",
        "From": "ops@example.com",
    }
    request = CrawlRequest(url="http://example.com/", policy={"headers": headers})
    assert request.policy.headers == headers


def test_conditional_headers_keep_their_specific_reason():
    """The reason survives the consolidation: conditionals point at validators."""
    from app.models.crawl_request import CrawlRequest

    with pytest.raises(ValidationError, match="validators"):
        CrawlRequest(url="http://example.com/", policy={"headers": {"If-None-Match": '"v1"'}})


#: Framing and hop-by-hop names. Not a second decision -- these belong to the connection, not to the caller's
#: request, and a caller that sets them is describing where one request ends and the next begins.
FRAMING_HEADERS = (
    "content-length",
    "transfer-encoding",
    "connection",
    "te",
    "trailer",
    "upgrade",
    "keep-alive",
)


@pytest.mark.parametrize("header", FRAMING_HEADERS)
def test_framing_headers_rejected_at_the_policy_chokepoint(header):
    """Rejected at the MODEL, not stripped at consumption.

    A silent strip leaves the caller believing the header was sent; the whole
    point of the chokepoint is that the refusal names the header and why."""
    from app.models.crawl_policy import CrawlPolicy

    with pytest.raises(ValidationError):
        CrawlPolicy(headers={header: "whatever"})


@pytest.mark.parametrize("header", FRAMING_HEADERS)
def test_framing_refusal_names_the_header_and_the_reason(header):
    """The message must identify WHICH header and WHY.

    "header not permitted" alone leaves a caller with a forty-header policy to
    bisect; the reason string turns the refusal into an explanation."""
    from app.models.crawl_policy import CrawlPolicy

    with pytest.raises(ValidationError) as exc:
        CrawlPolicy(headers={header: "whatever"})
    message = str(exc.value)
    assert header in message, message


def test_a_smuggling_attempt_is_refused_whole():
    """The realistic attack shape: the two framing headers that disagree.

    Sent together, Content-Length and Transfer-Encoding are the classic desync
    pair; the single cases prove each is refused."""
    from app.models.crawl_policy import CrawlPolicy

    with pytest.raises(ValidationError):
        CrawlPolicy(headers={"content-length": "10", "transfer-encoding": "chunked"})


def test_hop_by_hop_headers_are_refused_through_every_model_embedding_the_policy():
    """The ban must travel with the shared policy, exactly as the original six do.

    A ban enforced on CrawlPolicy alone would be bypassed by any model that
    embeds it, which is the same chokepoint gap this list exists to close.
    """
    import pytest as _pytest  # noqa: F401  (parametrize imported at module top)

    from app.models.crawl_request import CrawlRequest
    from app.models.job_create_request import JobCreateRequest
    from app.models.map_request import MapRequest

    for model, payload in (
        (CrawlRequest, {"url": "http://example.com/"}),
        (MapRequest, {"url": "http://example.com/"}),
        (JobCreateRequest, {"root_url": "http://example.com/"}),
    ):
        with pytest.raises(ValidationError):
            model(**payload, policy={"headers": {"transfer-encoding": "chunked"}})
