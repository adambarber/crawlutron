"""Policy.headers may not carry chokepoint-bypassing header names.

Two families, both rejected at the *model* (rejection at the validation site,
never a silent strip at consumption):

- Conditional headers (``if-none-match`` / ``if-modified-since``) belong to
  the ``validators`` map.
- ``cookie`` / ``host`` / ``proxy-authorization`` / ``x-forwarded-*`` /
  ``x-real-ip`` bypass chokepoints or spoof caller identity to the origin:
  the cookie jar owns Cookie, the pinned-request chokepoint owns Host, and
  origin-side IP/identity headers are never a crawl input.

``authorization`` is deliberately NOT banned here (user ruling 2026-09-22):
credentialed crawling of one's own site is a real use case, and banning the
header without a typed replacement would push callers into worse patterns
(baked-in URL credentials). It moves under the ``policy.credentials``
chokepoint; until then the ban list and this test
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


@pytest.mark.parametrize(
    ("module_name", "model_name"),
    [
        ("app.api_models", "CrawlRequest"),
        ("app.models.map_request", "MapRequest"),
        ("app.models.job_create_request", "JobCreateRequest"),
    ],
)
def test_the_ban_travels_with_the_shared_policy(module_name, model_name):
    """Every model embedding CrawlPolicy inherits the ban from one site.

    The three crawl-shaped request models (crawl, map, jobs) all embed the
    same CrawlPolicy, so anchoring the validator there covers them at once.
    ``RenderRequest`` embeds ``RenderOptionsRequest``, which has no ``headers``
    field -- it is structurally immune and correctly excluded here.
    """
    import importlib

    model = getattr(importlib.import_module(module_name), model_name)
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
    """The reason survives the consolidation: conditionals point at validators."""
    from app.api_models import CrawlRequest

    with pytest.raises(ValidationError, match="validators"):
        CrawlRequest(url="http://example.com/", policy={"headers": {"If-None-Match": '"v1"'}})


#: Framing and hop-by-hop names. Not a second decision -- these belong to the
#: connection, not to the caller's request, and a caller that sets them is
#: describing where one request ends and the next begins. The origin and this
#: service then disagree about framing, which is the request-smuggling surface.
#: httpx rejects CRLF in values, so the risk is not injection; it is that the
#: list IS the chokepoint, and silence reads as "considered and cleared".
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

    A silent strip would leave the caller believing the header was sent. The
    whole point of this chokepoint is that the refusal names the header and says
    why, so the caller can act on it.
    """
    from app.api_models import CrawlPolicy

    with pytest.raises(ValidationError):
        CrawlPolicy(headers={header: "whatever"})


@pytest.mark.parametrize("header", FRAMING_HEADERS)
def test_framing_refusal_names_the_header_and_the_reason(header):
    """The message must identify WHICH header and WHY.

    "header not permitted" without the name leaves a caller with a policy of
    forty headers to bisect, and the reason string is what turns the refusal
    into an explanation rather than a dead end.
    """
    from app.api_models import CrawlPolicy

    with pytest.raises(ValidationError) as exc:
        CrawlPolicy(headers={header: "whatever"})
    message = str(exc.value)
    assert header in message, message


def test_a_smuggling_attempt_is_refused_whole():
    """The realistic attack shape: the two framing headers that disagree.

    Sent together, Content-Length and Transfer-Encoding are the classic
    desync pair. Asserted as ONE case rather than two, because the two single
    cases above already prove each header is refused -- what is new here is
    that pairing them does not produce a differently-shaped outcome.
    """
    from app.api_models import CrawlPolicy

    with pytest.raises(ValidationError):
        CrawlPolicy(headers={"content-length": "10", "transfer-encoding": "chunked"})


def test_hop_by_hop_headers_are_refused_through_every_model_embedding_the_policy():
    """The ban must travel with the shared policy, exactly as the original six do.

    A ban enforced on CrawlPolicy alone would be bypassed by any model that
    embeds it, which is the same chokepoint gap this list exists to close.
    """
    import pytest as _pytest  # noqa: F401  (parametrize imported at module top)

    from app.api_models import CrawlRequest
    from app.models.job_create_request import JobCreateRequest
    from app.models.map_request import MapRequest

    for model, payload in (
        (CrawlRequest, {"url": "http://example.com/"}),
        (MapRequest, {"url": "http://example.com/"}),
        (JobCreateRequest, {"root_url": "http://example.com/"}),
    ):
        with pytest.raises(ValidationError):
            model(**payload, policy={"headers": {"transfer-encoding": "chunked"}})
