"""The render phases a browser page passes through, as named functions.

Each phase takes the collaborators it needs (the driver, the pin rules, the
page) so the render loop reads as the sequence of phases a reviewer can name.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, NamedTuple, Protocol

from app.extraction.classify import classify_page
from app.models.normalized_url import NormalizedUrl
from app.transport.browser_cookies import harvest_session_cookies
from app.transport.browser_targets import (
    make_response_handler,
    make_ssrf_route_handler,
    make_ssrf_websocket_handler,
)
from app.vocabulary.browser import BrowserAction

if TYPE_CHECKING:
    from app.models.host_policy import HostPolicy
    from app.models.render_options import RenderOptions
    from app.models.taxonomy import Classification

import logging

logger = logging.getLogger(__name__)


class RenderPage(Protocol):
    """The page surface the render phases call; a double needs exactly this."""

    url: str

    async def route(self, pattern: str, handler: Callable[..., object]) -> object: ...

    def route_web_socket(self, pattern: str, handler: Callable[..., object]) -> object: ...

    def on(self, event: str, handler: Callable[..., object]) -> object: ...

    async def goto(self, url: str, wait_until: str = "load", **kwargs: object) -> object: ...

    async def set_extra_http_headers(self, headers: dict[str, str]) -> object: ...

    async def evaluate(self, expression: str, *args: object, **kwargs: object) -> object: ...

    async def screenshot(self, **kwargs: object) -> bytes: ...


class WireGuard(NamedTuple):
    """The collaborators the SSRF guards need: the sync pin, the policy, the budget."""

    pin_sync: Callable[..., tuple[str, dict[str, str]]]
    policy: HostPolicy
    timeout_seconds: float


class PdfOutcome(NamedTuple):
    """What extraction decided: content plus the method that produced it."""

    html: str | None
    text: str | None
    classification: Classification
    method: str


class ReleasePlan(NamedTuple):
    """What the render's ``finally`` releases: the page and the harvest inputs."""

    page: RenderPage
    url: NormalizedUrl
    profile: dict[str, object]


def _profile_str(profile: dict[str, object] | None, key: str) -> str | None:
    """The profile entry as the string the caller expects, or None."""
    value = (profile or {}).get(key)
    return value if isinstance(value, str) else None


def _profile_bool(profile: dict[str, object] | None, key: str) -> bool:
    """The profile entry truth-tested, exactly as the inline ``bool(...)`` read it."""
    return bool((profile or {}).get(key))


def _profile_actions(profile: dict[str, object] | None) -> list[BrowserAction]:
    """The profile's action chain, or an empty one."""
    value = (profile or {}).get("actions")
    return value if isinstance(value, list) else []


def _profile_selectors(profile: dict[str, object] | None) -> list[str]:
    """The profile's targeted-screenshot selectors, or none."""
    value = (profile or {}).get("screenshot_selectors")
    return value if isinstance(value, list) else []


async def wire_ssrf_guards(
    page: RenderPage,
    *,
    wire: WireGuard,
    profile: dict[str, object] | None,
    on_response: list[dict[str, object]],
) -> None:
    """Route-level SSRF guards plus XHR interception, before navigation."""
    await page.route(
        "**/*",
        make_ssrf_route_handler(
            _profile_str(profile, "resource_mode"),
            include_screenshot=_profile_bool(profile, "include_screenshot"),
            policy=wire.policy,
            pin=wire.pin_sync,
            timeout_seconds=wire.timeout_seconds,
        ),
    )
    ws_guard = make_ssrf_websocket_handler(policy=wire.policy, timeout_seconds=wire.timeout_seconds)
    ws_router = getattr(page, "route_web_socket", None)
    if ws_router is None:
        logger.warning("websocket_interception_unsupported")
    else:
        # route_web_socket is async in Playwright 1.62+ (verified from the
        # installed source); accept both sync and async for doubles.
        outcome = ws_router("**/*", ws_guard)
        if asyncio.iscoroutine(outcome):
            await outcome
    # Katana-style XHR/fetch interception.
    page.on("response", make_response_handler(on_response))


async def warmup_pages(
    page: RenderPage,
    *,
    wait_for_ready: Callable[[RenderPage], Awaitable[None]],
    warmup_urls: list[str],
    policy: HostPolicy,
) -> None:
    """Visit the warmup URLs (no Host header needed) before the real navigation."""
    for warmup_url in warmup_urls:
        normalized_warmup = NormalizedUrl(warmup_url, policy=policy)
        # The driver's goto takes text -- .raw projects at that external-API edge.
        await page.goto(normalized_warmup.raw, wait_until="domcontentloaded")
        await wait_for_ready(page)


async def navigate_with_host_override(
    page: RenderPage,
    *,
    goto_and_settle: Callable[[str], Awaitable[None]],
    pinned_url: str,
    goto_headers: dict[str, str],
    timeout_seconds: float,
) -> None:
    """Navigate under the pinned Host override, then clear it.

    The override travels via ``set_extra_http_headers`` (goto takes no
    headers); the wait honors the timeout floored at 1s.
    """
    navigation_timeout = max(1.0, float(timeout_seconds))
    if goto_headers:
        await page.set_extra_http_headers(goto_headers)
    await asyncio.wait_for(goto_and_settle(pinned_url), timeout=navigation_timeout)
    if goto_headers:
        await page.set_extra_http_headers({})


async def run_pre_extraction_chain(
    page: RenderPage,
    *,
    run_actions: Callable[[RenderPage, list[BrowserAction], str, float], Awaitable[None]],
    lazy_scroll: Callable[[RenderPage], Awaitable[None]],
    flatten_shadow_dom: Callable[[RenderPage], Awaitable[None]],
    accept_cookies: Callable[[RenderPage, bool], Awaitable[bool]],
    profile: dict[str, object] | None,
    url: str,
    budget_seconds: float,
) -> None:
    """Action chains, lazy scroll, shadow flattening, then cookie consent."""
    actions = _profile_actions(profile)
    if actions:
        await run_actions(page, actions, url, budget_seconds)
    await lazy_scroll(page)
    await flatten_shadow_dom(page)
    await accept_cookies(page, _profile_bool(profile, "auto_accept_cookie_consent"))


async def extract_with_pdf_fallback(
    page: RenderPage,
    *,
    html: str | None,
    text: str | None,
    classification: Classification,
    options: RenderOptions,
    looks_like: Callable[[str], bool],
    pdf_extractor: Callable[[NormalizedUrl], str],
    policy: HostPolicy,
) -> PdfOutcome:
    """DOM extraction, or PDF text when the page turned out to be a PDF.

    The post-navigation URL is re-validated; a blocking file read stays off
    the event loop like every other document-extraction path.
    """
    method = "dom_text"
    final_url = NormalizedUrl(page.url, policy=policy)
    if looks_like(final_url.raw) and not (text or "").strip():
        pdf_text = await asyncio.to_thread(pdf_extractor, final_url)
        html = html if options.include_html else None
        title = await page.evaluate("() => document.title")
        classification = classify_page(html, pdf_text, title=str(title))
        text = pdf_text
        method = "pdf_text"
    return PdfOutcome(html, text, classification, method)


async def attach_screenshots(
    page: RenderPage,
    metadata: dict[str, object],
    *,
    capture_element_shots: Callable[[RenderPage, list[str]], Awaitable[list[dict[str, object]]]],
    profile: dict[str, object] | None,
) -> None:
    """Optional full-page screenshot plus targeted element screenshots."""
    if _profile_bool(profile, "include_screenshot"):
        screenshot_bytes = await page.screenshot(full_page=True)
        metadata["screenshot"] = base64.b64encode(screenshot_bytes).decode("ascii")
    element_shots = _profile_selectors(profile)
    if element_shots:
        metadata["element_screenshots"] = await capture_element_shots(page, element_shots)


async def release_page(
    plan: ReleasePlan,
    *,
    harvest_timeout: float,
    close_page: Callable[[RenderPage], Awaitable[None]],
    note_render: Callable[[], None],
) -> None:
    """Harvest cookies (bounded, best-effort), close the page, count the render."""
    try:
        await asyncio.wait_for(harvest_session_cookies(plan.page, plan.url), timeout=harvest_timeout)
    except TimeoutError:
        pass
    await close_page(plan.page)
    # Increment render count for ALL attempts (success or failure) so
    # browser recycle logic bounds total attempts per browser instance.
    note_render()
