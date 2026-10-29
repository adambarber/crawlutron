"""The render phases a browser page passes through, as named functions.

Each phase takes the collaborators it needs (the driver, the pin rules, the
page) so the render loop reads as the sequence of phases a reviewer can name.
"""

from __future__ import annotations

import asyncio
import base64
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, NamedTuple, cast

from app.extraction.classify import classify_page
from app.models.browser_profile_record import BrowserProfileRecord
from app.models.crawl_result import CrawlResult

# Page identity: re-normalized against the policy at the result boundary.
from app.models.intercepted_xhr_record import InterceptedXhrRecord
from app.models.json_value import JsonValue
from app.models.normalized_url import NormalizedUrl
from app.models.result_factory import terminal_attempt
from app.models.screenshot_diagnostics import ScreenshotDiagnostics
from app.transport.browser_cookies import harvest_session_cookies
from app.transport.browser_driver import BrowserDriver
from app.transport.browser_targets import (
    make_response_handler,
    make_ssrf_route_handler,
    make_ssrf_websocket_handler,
)
from app.transport.render_page import RenderPage
from app.transport.timeouts import render_phase_seconds
from app.vocabulary.browser import BrowserAction

if TYPE_CHECKING:
    from app.models.host_policy import HostPolicy
    from app.models.render_options import RenderOptions
    from app.models.taxonomy import Classification
    from app.vocabulary.taxonomy import ResultStatus

import logging

logger = logging.getLogger(__name__)


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
    profile: BrowserProfileRecord


def _profile_str(profile: BrowserProfileRecord | None, key: str) -> str | None:
    """The profile entry as the string the caller expects, or None."""
    value = (profile or {}).get(key)
    return value if isinstance(value, str) else None


def _profile_bool(profile: BrowserProfileRecord | None, key: str) -> bool:
    """The profile entry truth-tested, exactly as the inline ``bool(...)`` read it."""
    return bool((profile or {}).get(key))


def _profile_actions(profile: BrowserProfileRecord | None) -> list[BrowserAction]:
    """The profile's action chain, or an empty one."""
    value = (profile or {}).get("actions")
    return cast(list[BrowserAction], value) if isinstance(value, list) else []


def _profile_selectors(profile: BrowserProfileRecord | None) -> list[str]:
    """The profile's targeted-screenshot selectors, or none."""
    value = (profile or {}).get("screenshot_selectors")
    return value if isinstance(value, list) else []


async def wire_ssrf_guards(
    page: RenderPage,
    *,
    wire: WireGuard,
    profile: BrowserProfileRecord | None,
    on_response: list[InterceptedXhrRecord],
) -> None:
    """Route-level SSRF guards plus XHR interception, before navigation."""
    # Install at the page's context: every page/worker the context spawns is
    # covered, including ones created after the install.
    context = page.context
    await context.route(
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
    ws_router = getattr(context, "route_web_socket", None)
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
    timeout_seconds: float,
) -> None:
    """Visit the warmup URLs (no Host header needed) before the real navigation.

    Every warmup goto carries the one render-phase budget; before the
    budget existed the navigation was unbounded (Playwright's default).
    """
    warmup_budget = render_phase_seconds(timeout_seconds)
    for warmup_url in warmup_urls:
        normalized_warmup = NormalizedUrl(warmup_url, policy=policy)
        # The driver's goto takes text -- .raw projects at that external-API edge.
        await page.goto(normalized_warmup.raw, wait_until="domcontentloaded", timeout=warmup_budget)
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
    navigation_timeout = render_phase_seconds(timeout_seconds)
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
    profile: BrowserProfileRecord | None,
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
    pdf_extractor: Callable[..., str],
    policy: HostPolicy,
) -> PdfOutcome:
    """DOM extraction, or PDF text when the page turned out to be a PDF.

    The post-navigation URL is re-validated; a blocking file read stays off
    the event loop like every other document-extraction path.
    """
    method = "dom_text"
    final_url = NormalizedUrl(page.url, policy=policy)
    if looks_like(final_url.raw) and not (text or "").strip():
        # A lambda because asyncio.to_thread rejects the extra argument under
        # mypy; the spy records the closure's callee via its code name.
        pdf_text = await asyncio.to_thread(
            lambda: pdf_extractor(final_url, render_phase_seconds(options.timeout_seconds))
        )
        html = html if options.include_html else None
        title = await page.evaluate("() => document.title")
        classification = classify_page(html, pdf_text, title=str(title))
        text = pdf_text
        method = "pdf_text"
    return PdfOutcome(html, text, classification, method)


async def attach_screenshots(
    page: RenderPage,
    metadata: dict[str, JsonValue],
    *,
    capture_element_shots: Callable[[RenderPage, list[str]], Awaitable[list[ScreenshotDiagnostics]]],
    profile: BrowserProfileRecord | None,
) -> None:
    """Optional full-page screenshot plus targeted element screenshots."""
    if _profile_bool(profile, "include_screenshot"):
        screenshot_bytes = await page.screenshot(full_page=True)
        metadata["screenshot"] = base64.b64encode(screenshot_bytes).decode("ascii")
    element_shots = _profile_selectors(profile)
    if element_shots:
        # The shots are bounded JSON records (selector + base64 string).
        metadata["element_screenshots"] = cast(JsonValue, await capture_element_shots(page, element_shots))


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


class RenderCollaborators(NamedTuple):
    """The renderer's callables and rules the render phases share.

    One bundle instead of nine mirrored keyword walls per phase (the clone
    gate counts those); a named record reads better than a tenth line.
    """

    policy: HostPolicy
    wire: WireGuard
    warmup_urls: list[str]
    driver: BrowserDriver
    navigate_and_settle: Callable[[str], Awaitable[None]]
    run_actions: Callable[..., Awaitable[None]]
    extract: Callable[..., Awaitable[tuple[str | None, str | None, Classification, bool]]]
    looks_like: Callable[[str], bool]
    pdf_extractor: Callable[..., str]
    status_for: Callable[[str], ResultStatus]


async def guarded_render(
    page: RenderPage,
    url: NormalizedUrl,
    options: RenderOptions,
    *,
    pinned_url: str,
    goto_headers: dict[str, str],
    started: float,
    collaborators: RenderCollaborators,
) -> CrawlResult:
    """The guarded render setup: wire guards, warm up, navigate to the pin."""
    intercepted_xhr: list[InterceptedXhrRecord] = []
    await wire_ssrf_guards(
        page, wire=collaborators.wire, profile=options.browser_profile, on_response=intercepted_xhr
    )
    await warmup_pages(
        page,
        wait_for_ready=collaborators.driver.wait_for_ready_state,
        warmup_urls=collaborators.warmup_urls,
        policy=collaborators.policy,
        timeout_seconds=options.timeout_seconds,
    )
    await navigate_with_host_override(
        page,
        goto_and_settle=lambda pinned: collaborators.navigate_and_settle(pinned),
        pinned_url=pinned_url,
        goto_headers=goto_headers,
        timeout_seconds=options.timeout_seconds,
    )
    return await render_and_extract(
        page,
        url,
        options,
        started=started,
        collaborators=collaborators,
        intercepted_xhr=intercepted_xhr,
    )


async def render_and_extract(
    page: RenderPage,
    url: NormalizedUrl,
    options: RenderOptions,
    *,
    started: float,
    collaborators: RenderCollaborators,
    intercepted_xhr: list[InterceptedXhrRecord],
) -> CrawlResult:
    """The interaction chain, extraction, PDF fallback, and result shaping."""
    driver, policy = collaborators.driver, collaborators.policy
    await run_pre_extraction_chain(
        page,
        run_actions=collaborators.run_actions,
        lazy_scroll=driver.lazy_load_scroll,
        flatten_shadow_dom=driver.flatten_shadow_dom,
        accept_cookies=driver.auto_accept_cookie_consent,
        profile=options.browser_profile,
        url=url.raw,
        budget_seconds=render_phase_seconds(options.timeout_seconds),
    )

    html, text, classification, challenge_resolved = await collaborators.extract(page, options)
    outcome = await extract_with_pdf_fallback(
        page,
        html=html,
        text=text,
        classification=classification,
        options=options,
        looks_like=collaborators.looks_like,
        pdf_extractor=collaborators.pdf_extractor,
        policy=policy,
    )
    return await shape_render_result(
        page,
        url,
        options=options,
        policy=policy,
        outcome=outcome,
        challenge_resolved=challenge_resolved,
        intercepted_xhr=intercepted_xhr,
        started=started,
        status_for=collaborators.status_for,
        driver_reused=driver.reused,
        capture_element_screenshots=driver.capture_element_screenshots,
    )


async def shape_render_result(
    page: RenderPage,
    url: NormalizedUrl,
    *,
    options: RenderOptions,
    policy: HostPolicy,
    outcome: PdfOutcome,
    challenge_resolved: bool,
    intercepted_xhr: list[InterceptedXhrRecord],
    started: float,
    status_for: Callable[[str], ResultStatus],
    driver_reused: bool,
    capture_element_screenshots: Callable[[RenderPage, list[str]], Awaitable[list[ScreenshotDiagnostics]]],
) -> CrawlResult:
    """Terminal status, metadata, screenshots, and the CrawlResult envelope."""
    status = status_for(outcome.classification)
    metadata: dict[str, JsonValue] = {
        "title": cast(JsonValue, await page.evaluate("() => document.title")),
        "content_length": len(outcome.html or ""),
        "browser_reused": driver_reused,
        "challenge_resolved": challenge_resolved,
        "extraction_method": outcome.method,
        # The handler's records are JSON by construction (bounded keys, JSON payload).
        "intercepted_xhr": cast(JsonValue, intercepted_xhr),
    }
    await attach_screenshots(
        page,
        metadata,
        capture_element_shots=capture_element_screenshots,
        profile=options.browser_profile,
    )
    return CrawlResult(
        url=url,
        attempt=terminal_attempt(
            strategy=None,
            status=status,
            classification=outcome.classification,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            error=None,
        ),
        final_url=NormalizedUrl(page.url, policy=policy),
        html=outcome.html,
        text=outcome.text,
        attempts=1,
        domain=url.site,
        metadata=metadata,
    )
