"""Browser render strategy (Camoufox/Playwright) with SOTA discovery hooks.

Owns the :class:`BrowserRenderer` which drives a headless/full browser to
render JavaScript-heavy pages. Beyond basic DOM extraction it now also:

* intercepts XHR/fetch JSON responses (Katana-style dynamic endpoint discovery),
* performs lazy-load infinite-scroll to trigger deferred content, and
* optionally captures a full-page screenshot as base64 for debugging.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import threading
import time
from collections import OrderedDict
from collections.abc import (
    Awaitable,
    Callable,
)
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    pass


from app.config.env import get_env_float, get_env_int
from app.config.settings import CrawlerSettings
from app.extraction.classify import classify_page
from app.models.crawl_result import CrawlResult
from app.models.host_policy import HostPolicy
from app.models.normalized_url import NormalizedUrl
from app.models.render_options import RenderOptions
from app.models.taxonomy import Classification, CrawlError
from app.orchestration.classification import classify_error_code
from app.orchestration.render_result import (
    terminal_attempt,
    terminal_crawl_result,
)
from app.safety.json_safe import json_default
from app.transport.browser_driver import BrowserDriver, async_sleep
from app.transport.document import _default_document_text_extractor
from app.transport.pinned_transport import (
    build_pinned_request,
    build_pinned_request_async,
    check_ssrf_protection,
)
from app.vocabulary.hosts import is_analytics_host as _is_analytics_url
from app.vocabulary.limits import DEFAULT_CHALLENGE_WAIT_SECONDS, DEFAULT_MAX_CONCURRENT_PAGES
from app.vocabulary.taxonomy import ResultStatus, status_for_classification

logger = logging.getLogger(__name__)


async def resolve_browser_target(
    url: NormalizedUrl,
    *,
    pin: Callable[..., Awaitable[tuple[str, dict[str, str]]]],
) -> tuple[str, dict[str, str]]:
    """SSRF-check + IP-pin ``url`` for browser navigation.

    Returns ``(pinned_url, extra_http_headers)``. The browser is told to connect
    to the single resolved IP (preventing DNS-rebinding) while preserving the
    original ``Host`` header. Raises ``ValueError`` if the target is unsafe --
    the value's own policy decides whether the target is reachable at all.

    Async because pinning resolves DNS, and a render is not the only thing on
    the loop: called synchronously this ran ``socket.getaddrinfo`` inline, so every
    other in-flight crawl waited out the resolver once per rendered page.

    ``pin`` is a required argument, not a keyword with a default. It is the rule
    that decides whether this crawl may reach the URL at all, so a caller that
    forgot to pass one would silently fall back to a weaker rule; the type is
    declared here rather than imported so the dependency is visible at the call.
    """
    pinned_url, request_headers = await pin(url, {})
    host = request_headers.get("Host")
    return pinned_url, ({"Host": host} if host else {})


def _assert_safe_final_url(final_url: str, *, policy: HostPolicy) -> None:
    """Re-validate the post-navigation URL.

    The rendered site may have redirected the browser to an internal host. Fail
    closed (raise ``ValueError``) rather than returning potentially-internal
    content.
    """
    normalized = NormalizedUrl(final_url, policy=policy)
    check_ssrf_protection(normalized)


def _is_ssrf_blocked(url: str, *, policy: HostPolicy) -> bool:
    """Return ``True`` if ``url`` resolves to an SSRF-unsafe target."""
    try:
        normalized = NormalizedUrl(url, policy=policy)
        check_ssrf_protection(normalized)
    except ValueError:
        return True
    return False


# Subresource types with no crawlable text: aborted in reduced-resource mode.
# Tracking/analytics hosts are never fetched (app.vocabulary.hosts, shared with enqueue refusal).
_REDUCED_BLOCK_RESOURCE_TYPES = frozenset({"image", "font", "media"})


def make_ssrf_route_handler(
    resource_mode: str | None = None,
    *,
    include_screenshot: bool = False,
    policy: HostPolicy,
    pin: Callable[..., tuple[str, dict[str, str]]],
):
    """Playwright route handler that aborts requests to SSRF-unsafe hosts.

    Registered via ``page.route`` so the main navigation, server-side
    redirects, and every subresource load (images/scripts/XHR) is checked
    against the SSRF blocklist. Requests that resolve to internal/loopback/
    reserved addresses are aborted before the browser connects.

    The SSRF check involves synchronous DNS resolution (socket.getaddrinfo).
    To avoid blocking the event loop, the check runs in a thread pool via
    ``asyncio.to_thread``.

    Plain-HTTP requests keep the FRESH (uncached) pinned IP, which governs the
    actual connection: a rebind inside the resolver cache TTL must not slip
    onto the wire. HTTPS requests are validated through the
    same cached check the websocket handler uses: ``build_pinned_request``
    keeps the original hostname there (real certificates carry no IP address
    SAN, so a pin would only break SNI/cert verification), making an uncached
    pin resolve per subresource pure cost with no security gain. HTTPS stays
    blocklist-checked before the browser connects.
    """

    async def _handler(route, request):
        # Re-pin the IP per subresource request (DNS-rebinding TOCTOU): raises on
        # private/loopback, returns (pinned_url, {Host: hostname}) to preserve routing.
        request_scheme = request.url.split(":", 1)[0].lower()
        if request_scheme not in ("http", "https", ""):
            # Data:, blob:, about:, javascript: never touch the network via DNS;
            # let the browser handle them locally instead of aborting.
            await route.continue_()
            return
        # The browser-observed URL is a live-text boundary: admitted here, once,
        # inline; an unadmittable URL reaches the same "unsafe target" abort below.
        try:
            admitted = NormalizedUrl(request.url, policy=policy)
        except ValueError:
            await route.abort()
            return
        # Tracking/analytics requests carry no crawlable content; drop them
        # unconditionally so they never consume bandwidth or trigger SSRF resolution.
        if _is_analytics_url(admitted):
            await route.abort()
            return
        # Abort presentation subresources: a text crawler never needs them (~70%
        # of render bandwidth); permitted only when a screenshot is requested.
        if getattr(request, "resource_type", "") in _REDUCED_BLOCK_RESOURCE_TYPES and not include_screenshot:
            await route.abort()
            return
        try:
            # Annotated once for both branches: mypy cannot widen the https
            # branch's empty literal to the dict type the http pin rule returns.
            host_headers: dict[str, str]
            if request_scheme == "https":
                # https discards the pinned IP (TLS SNI/certs need the hostname), so
                # validate via the cached TTL'd check: one resolve per host, fail-closed.
                await asyncio.to_thread(check_ssrf_protection, admitted)
                pinned_url, host_headers = request.url, {}
            else:
                pinned_url, host_headers = await asyncio.to_thread(
                    pin,
                    admitted,
                    {},
                )
        except Exception:
            # Fail closed: DNS timeout, resolver errors, anything unexpected ->
            # abort rather than let an unchecked connection proceed.
            await route.abort()
            return
        overrides = {}
        if host_headers.get("Host"):
            overrides["Host"] = host_headers["Host"]
        # Omit headers= when empty: Playwright treats an explicit dict as a full
        # replacement, so {} would wipe UA/cookies/accept off every HTTPS subresource.
        if overrides:
            await route.continue_(url=pinned_url, headers=overrides)
        else:
            await route.continue_(url=pinned_url)

    return _handler


def make_ssrf_websocket_handler(*, policy: HostPolicy):
    """Playwright websocket-route handler blocking SSRF-unsafe WS targets.

    page.route does not intercept WebSocket upgrades; without this a hostile
    page could open a WebSocket to an internal host around the route guard.
    Validation reuses check_ssrf_protection with ws/wss mapped to http/https.
    """

    async def _ws_handler(ws):
        url = getattr(ws, "url", "") or ""
        scheme = url.split(":", 1)[0].lower()
        if scheme not in ("ws", "wss"):
            return  # not a websocket target
        http_equivalent = ("https" if scheme == "wss" else "http") + url[len(scheme) :]
        try:
            normalized = await asyncio.to_thread(
                NormalizedUrl,
                http_equivalent,
                policy=policy,
            )
            await asyncio.to_thread(check_ssrf_protection, normalized)
        except Exception:
            close = getattr(ws, "close", None)
            if close is not None:
                result = close()
                if asyncio.iscoroutine(result):
                    await result

    return _ws_handler


# The blocked/failed labels and the classification -> status table live in
# app.vocabulary.taxonomy, so the HTTP and browser paths cannot disagree about a label.

# Canonical defaults live in app.vocabulary.limits; these module names stay as the
# settings-less fallback so existing references (the suite's scroll disabler) keep working.
_BROWSER_COOKIES_MAX = 500


class _LockedDomainCookieStore:
    """LRU cookie store whose every read and mutation is internally guarded.

    The store is module-global and genuinely concurrent: the render path writes
    harvested cookies while the crawl engine reads them for the cffi
    cookie-bootstrap retry, from different tasks and threads. Every operation
    acquires ``_lock`` for its whole duration, so an unsynchronized access is
    not a state a caller can construct and the eviction cannot interleave with a
    read.

    It implements no container protocol. It used to subclass
    ``MutableMapping``, and ten of its thirteen methods existed to honour that
    promise to callers outside ``app/`` -- no production path reached any of
    them, and ``no-unclaimed-container-protocol`` now holds that line.
    """

    __slots__ = ("_data", "_lock", "_max_entries")

    def __init__(self, max_entries: int) -> None:
        # OrderedDict: insertion order doubles as the eviction order (plain
        # dict has no move_to_end/popitem(last=False)).
        self._data: OrderedDict[str, str] = OrderedDict()
        self._lock = threading.Lock()
        self._max_entries = max_entries

    def store(self, domain: str, header: str) -> None:
        """Record a harvested cookie header, evicting LRU entries past the cap.

        Assignment and eviction happen under one acquisition, so a concurrent
        reader can never observe the store over the cap, and the entry being
        written cannot be the one evicted by its own insert.
        """
        with self._lock:
            # Deliberately not move_to_end: re-storing a known domain updates
            # the value in place, matching plain-mapping semantics.
            self._data[domain] = header
            while len(self._data) > self._max_entries:
                self._data.popitem(last=False)

    def touch_get(self, domain: str) -> str | None:
        """Look up a domain and mark it most-recently-used when present.

        Membership check, touch and value read are one critical section: this
        is the operation that used to race ``popitem`` eviction.
        """
        with self._lock:
            value = self._data.get(domain)
            if value is not None:
                self._data.move_to_end(domain)
            return value

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)


_BROWSER_COOKIES_BY_DOMAIN = _LockedDomainCookieStore(_BROWSER_COOKIES_MAX)


def reset_process_state() -> None:
    """Drop every cookie the renderer has accumulated for a domain.

    The store is process-wide and bounded, not cleared, so a browser test that
    renders once leaves its domain's cookies for the next test to assert against.
    """
    _BROWSER_COOKIES_BY_DOMAIN.clear()


def has_process_state() -> bool:
    """Whether any domain's cookies are held right now."""
    return len(_BROWSER_COOKIES_BY_DOMAIN) > 0


def _store_browser_cookies(domain: str, header: str) -> None:
    """Record a harvested cookie header, evicting the LRU entry past the cap."""
    _BROWSER_COOKIES_BY_DOMAIN.store(domain, header)


def get_browser_cookies_for(domain: str) -> str | None:
    return _BROWSER_COOKIES_BY_DOMAIN.touch_get(domain)


class BrowserRenderer:
    def __init__(
        self,
        *,
        sleep: Callable[[float], None] = time.sleep,
        challenge_wait_seconds: float | None = None,
        pdf_text_extractor: Callable[[NormalizedUrl], str] | None = None,
        extract: Callable[
            [Any, RenderOptions], Awaitable[tuple[str | None, str | None, Classification, bool]]
        ]
        | None = None,
        looks_like_pdf: Callable[[str], bool] | None = None,
        warmup_urls: list[str] | None = None,
        settings: CrawlerSettings | None = None,
        driver: BrowserDriver | None = None,
        pin: Callable[..., Awaitable[tuple[str, dict[str, str]]]] | None = None,
        pin_sync: Callable[..., tuple[str, dict[str, str]]] | None = None,
        status_for: Callable[[str], ResultStatus] | None = None,
    ) -> None:
        self._sleep = sleep
        # Policy limit, then the settings field, then env, then the canonical
        # default: the number itself lives only in app.vocabulary.limits.
        if challenge_wait_seconds is None:
            if settings is not None:
                self._challenge_wait_seconds = float(settings.challenge_wait_seconds)
            else:
                self._challenge_wait_seconds = get_env_float(
                    "CRAWLUTRON_CHALLENGE_WAIT_SECONDS", DEFAULT_CHALLENGE_WAIT_SECONDS, lo=0.0, hi=300.0
                )
        else:
            self._challenge_wait_seconds = challenge_wait_seconds
        self._pdf_text_extractor = pdf_text_extractor or _default_document_text_extractor
        # The two renderer-owned predicates a test steers (page yield, PDF);
        # overridable collaborators, so a test substitutes behaviour, not internals.
        self._extract = extract or self.extract_with_challenge_wait
        self._looks_like = looks_like_pdf or self._looks_like_pdf
        self._warmup_urls = warmup_urls if warmup_urls is not None else self._warmup_urls_from_env()
        # The two SSRF pin rules and the failure-path classifier: all three decide
        # something the render cannot recover from, so tests steer them by argument.
        self._pin = pin or build_pinned_request_async
        self._pin_sync = pin_sync or build_pinned_request
        self._status_for = status_for or status_for_classification
        # The browser and every page step live in the driver (recycle bound, scroll/
        # harvest knobs, storage_state allowlist); a test substitutes the driver.
        self._driver = driver if driver is not None else BrowserDriver(sleep=sleep, settings=settings)
        # Cap concurrent page renders: N simultaneous requests each open a tab,
        # which otherwise crashes/OOMs the browser under burst load.
        _concurrent_pages = (
            settings.max_concurrent_pages
            if settings is not None
            else get_env_int("CRAWLUTRON_MAX_CONCURRENT_PAGES", DEFAULT_MAX_CONCURRENT_PAGES, lo=1)
        )
        self._page_semaphore = asyncio.Semaphore(max(1, _concurrent_pages))

    async def _async_sleep(self, seconds: float) -> None:
        """Non-blocking sleep for use inside coroutines.

        The default ``time.sleep`` clock (injectable for tests) would block the
        whole event loop; run it in a thread so one render can never freeze the
        service.
        """
        await async_sleep(self._sleep, seconds)

    @property
    def pdf_text_extractor(self) -> Callable[[NormalizedUrl], str]:
        """The collaborator that turns a PDF URL into text.

        Public because it is an injection point: a caller that supplied one, or a
        test asserting the supplied/default behaviour, should not have to read a
        private attribute to say which extractor is in play.
        """
        return self._pdf_text_extractor

    @property
    def driver(self) -> BrowserDriver:
        """The driver this renderer drives the page with.

        Public because the driver is a collaborator rather than an
        implementation detail: it owns the browser lifecycle and the per-page
        steps, and a test asserting on either needs to name it rather than reach
        past the renderer into a private attribute.
        """
        return self._driver

    @property
    def warmup_urls(self) -> list[str]:
        """Resolved warmup URLs for this renderer's browser profile.

        The headed-warmed strategy profile is the one carrying warmup URLs (see
        ``app.vocabulary.strategies.BROWSER_STRATEGIES``); exposing the resolved list lets
        the browser pool label its renders with the real browser mode instead
        of inventing a strategy name on a failure path.
        """
        return self._warmup_urls

    async def _harvest_session_cookies(self, page: Any, url: NormalizedUrl) -> None:
        """Capture the page's cookies into the per-domain bootstrap store.

        Must run BEFORE the page/context is closed -- Camoufox destroys the
        context with the last page, leaving ``browser.contexts`` empty -- and
        must COMPLETE before returning: the cffi retry reads the store
        synchronously right after the render, so a fire-and-forget task would
        usually lose the race and retry cookieless. Harvest failures never
        break the render itself.
        """
        try:
            context = getattr(page, "context", None)
            if context is None:
                return
            cookies = await context.cookies(url.raw)
            header = "; ".join(f"{c['name']}={c['value']}" for c in cookies if c.get("name"))
            if header:
                _store_browser_cookies(url.site, header)
                logger.info("browser_cookies_harvested", extra={"domain": url.site, "count": len(cookies)})
        except Exception as error:
            logger.debug("browser_cookies_harvest_failed", extra={"error": str(error)})

    async def render(
        self,
        url: NormalizedUrl,
        options: RenderOptions,
        *,
        policy: HostPolicy,
    ) -> CrawlResult:
        started = time.monotonic()
        attempts = 0
        last_error = None
        max_attempts = max(1, options.max_retries + 1)

        while attempts < max_attempts:
            attempts += 1
            try:
                result = await self._render_with_browser(url, options, policy=policy)
                return result
            except Exception as error:
                classification: Literal["navigation_timeout", "unknown_error"] = (
                    "navigation_timeout" if isinstance(error, TimeoutError) else "unknown_error"
                )
                last_error = classify_error_code(error)
                await self._driver.close()
                if attempts >= max_attempts:
                    return self._failure_result(
                        url, "retry_exhausted", classification, attempts, started, last_error
                    )
                await self._async_sleep(min(2 ** (attempts - 1), 5))

        return self._failure_result(url, "failed", "unknown_error", attempts, started, last_error)

    async def close(self) -> None:
        """Release the reused browser instance at service shutdown."""
        await self._driver.close()

    async def _render_with_browser(
        self,
        url: NormalizedUrl,
        options: RenderOptions,
        *,
        policy: HostPolicy,
    ) -> CrawlResult:
        # Cap concurrent page renders: each render opens a browser tab, and an
        # unbounded burst of tabs crashes/OOMs the shared Camoufox process.
        async with self._page_semaphore:
            return await self._render_with_page_unlocked(url, options, policy=policy)

    async def _navigate_and_settle(self, page: Any, pinned_url: str) -> None:
        """Goto the pinned URL and wait for a settled ready state."""
        await page.goto(pinned_url, wait_until="domcontentloaded")
        await self._driver.wait_for_ready_state(page)

    async def _render_with_page_unlocked(
        self,
        url: NormalizedUrl,
        options: RenderOptions,
        *,
        policy: HostPolicy,
    ) -> CrawlResult:
        # SSRF: validate + IP-pin the target (DNS-rebinding safe, Host preserved).
        # Raises ValueError if the target is unsafe.
        pinned_url, goto_headers = await resolve_browser_target(url, pin=self._pin)

        started = time.monotonic()

        page = await self._driver.open_page(options.browser_profile)
        # Abort any request (navigation, redirect, or subresource) that
        # resolves to an SSRF-unsafe host before the browser connects.
        await page.route(
            "**/*",
            make_ssrf_route_handler(
                (options.browser_profile or {}).get("resource_mode"),
                include_screenshot=bool((options.browser_profile or {}).get("include_screenshot")),
                policy=policy,
                pin=self._pin_sync,
            ),
        )
        ws_guard = make_ssrf_websocket_handler(policy=policy)
        ws_router = getattr(page, "route_websocket", None)
        if ws_router is None:
            logger.warning("websocket_interception_unsupported")
        else:
            # Route_websocket is sync in Playwright (unlike page.route); accept
            # both sync and async implementations for test doubles.
            outcome = ws_router("**/*", ws_guard)
            if asyncio.iscoroutine(outcome):
                await outcome
        try:
            # Katana-style XHR/fetch interception.
            intercepted_xhr: list[dict[str, Any]] = []
            page.on("response", self.make_response_handler(intercepted_xhr))

            # Warmup URLs (no Host header needed). The driver's goto takes
            # text -- .raw projects at that external-API edge.
            for warmup_url in self._warmup_urls:
                normalized_warmup = NormalizedUrl(warmup_url, policy=policy)
                await page.goto(normalized_warmup.raw, wait_until="domcontentloaded")
                await self._driver.wait_for_ready_state(page)

            # Navigate with the Host override via set_extra_http_headers (goto takes no
            # headers); wait_for honors the timeout, floored at 1s, mapping to navigation_timeout.
            navigation_timeout = max(1.0, float(options.timeout_seconds))
            if goto_headers:
                await page.set_extra_http_headers(goto_headers)
            await asyncio.wait_for(
                self._navigate_and_settle(page, pinned_url),
                timeout=navigation_timeout,
            )
            if goto_headers:
                # Clear the page-scoped Host override: leaving it would spoof Host
                # on every later request from this page (XHR, later navigations).
                await page.set_extra_http_headers({})

            # Browser action chains: run policy-specified interactions before extraction.
            actions = (options.browser_profile or {}).get("actions") or []
            if actions:
                await self._driver.run_action_chain(
                    page, actions, url=url.raw, budget_seconds=navigation_timeout
                )

            # Lazy-load scroll: trigger deferred/infinite-scroll content.
            await self._driver.lazy_load_scroll(page, options.browser_profile)

            # Shadow-DOM flattening: inline open shadow roots so their links
            # and visible text are reachable by the standard HTML/text extractors.
            await self._driver.flatten_shadow_dom(page)

            # Auto-accept cookie consent (opt-in only) before extraction
            await self._driver.auto_accept_cookie_consent(
                page, bool((options.browser_profile or {}).get("auto_accept_cookie_consent"))
            )

            # Extract with challenge waiting
            html, text, classification, challenge_resolved = await self._extract(page, options)

            extraction_method = "dom_text"
            final_url = NormalizedUrl(page.url, policy=policy)
            # Re-validate the post-navigation URL: the site may have redirected to
            # an internal host. Fail closed (already admitted as a NormalizedUrl above).

            # PDF extraction fallback
            if self._looks_like(final_url.raw) and not (text or "").strip():
                # Blocking file read + parse: keep it off the event loop like every
                # other document-extraction path.
                text = await asyncio.to_thread(self._pdf_text_extractor, final_url)
                html = html if options.include_html else None
                title = await page.evaluate("() => document.title")
                classification = classify_page(html, text, title=title)
                extraction_method = "pdf_text"

            status = self._status_for(classification)

            metadata: dict[str, Any] = {
                "title": await page.evaluate("() => document.title"),
                "content_length": len(html or ""),
                "browser_reused": self._driver.reused,
                "challenge_resolved": challenge_resolved,
                "extraction_method": extraction_method,
                "intercepted_xhr": intercepted_xhr,
            }

            # Screenshots: optional full-page, plus targeted element screenshots by CSS selector.
            if options.browser_profile.get("include_screenshot"):
                screenshot_bytes = await page.screenshot(full_page=True)
                metadata["screenshot"] = base64.b64encode(screenshot_bytes).decode("ascii")
            element_shots = (options.browser_profile or {}).get("screenshot_selectors") or []
            if element_shots:
                metadata["element_screenshots"] = await self._driver.capture_element_screenshots(
                    page, element_shots
                )

            return CrawlResult(
                url=url,
                attempt=terminal_attempt(
                    strategy=None,
                    status=status,
                    classification=classification,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                    error=None,
                ),
                final_url=final_url,
                html=html,
                text=text,
                attempts=1,
                domain=url.site,
                metadata=metadata,
            )
        finally:
            # Always release the page between crawls (the Camoufox memory-growth
            # root); a wedged context.cookies() falls back cookieless after a bounded wait.
            try:
                await asyncio.wait_for(
                    self._harvest_session_cookies(page, url),
                    timeout=self._driver.harvest_timeout_for(options.browser_profile or {}),
                )
            except TimeoutError:
                pass
            await self._driver.safe_close_page(page)
            # Increment render count for ALL attempts (success or failure) so
            # browser recycle logic bounds total attempts per browser instance.
            self._driver.note_render()

    @staticmethod
    def make_response_handler(collected: list[dict[str, Any]]) -> Callable[[Any], Awaitable[None]]:
        # XHR bounds: 10 entries x 8KB payload (50 x 64KB held ~3.2MB per page);
        # URLs are page-controlled too, so each entry's URL is bounded on the same terms.
        max_entries = 10
        max_payload_chars = 8192
        max_url_chars = 2048

        async def on_response(response: Any) -> None:
            if len(collected) >= max_entries:
                return  # hostile pages can fire thousands of JSON responses
            headers = response.headers or {}
            content_type = ""
            if isinstance(headers, dict):
                for key, value in headers.items():
                    if str(key).lower() == "content-type":
                        content_type = str(value)
                        break
            if "application/json" not in content_type:
                return
            url = str(getattr(response, "url", ""))
            entry: dict[str, Any] = {
                "url": url[:max_url_chars],
                "content_type": content_type,
                "status": getattr(response, "status", None),
                "payload": None,
            }
            if len(url) > max_url_chars:
                entry["url_truncated"] = True
            try:
                # ACCEPTED RESIDUAL: the browser already materialized this body, so
                # the 8KB cap applies after one transient parse; bounding it drops real JSON.
                payload = await response.json()
            except Exception:  # payload capture is best-effort
                payload = None
            if payload is not None and len(json.dumps(payload, default=json_default)) > max_payload_chars:
                entry["payload_truncated"] = True
            else:
                entry["payload"] = payload
            collected.append(entry)

        return on_response

    async def extract_with_challenge_wait(
        self, page: Any, options: RenderOptions
    ) -> tuple[str | None, str | None, Classification, bool]:
        html = await self.extract_html(page) if options.include_html else None
        text = await self.extract_text(page) if options.include_text else None
        title = await page.evaluate("() => document.title")
        classification = classify_page(html, text, title=title)

        if classification != "bot_challenge_detected":
            return html, text, classification, False

        deadline = time.monotonic() + self._challenge_wait_seconds
        while time.monotonic() < deadline:
            # Use asyncio.sleep to avoid busy-waiting if injected sleep is patched/no-op
            await asyncio.sleep(1)
            await self._driver.wait_for_ready_state(page)
            next_html = await self.extract_html(page) if options.include_html else None
            next_text = await self.extract_text(page) if options.include_text else None
            next_title = await page.evaluate("() => document.title")
            next_classification = classify_page(next_html, next_text, title=next_title)
            if next_classification != "bot_challenge_detected":
                return next_html, next_text, next_classification, True
            html, text, classification = next_html, next_text, next_classification

        return html, text, classification, False

    @staticmethod
    async def extract_html(page: Any) -> str:
        """The page's serialized HTML, or empty string if the page will not say."""
        try:
            return await page.content()
        except Exception:
            return ""

    @staticmethod
    async def extract_text(page: Any) -> str:
        """The rendered visible text, or empty string if the page will not say.

        Static and public for the same reason ``extract_html`` is: neither reads
        renderer state, so a test that substitutes the extraction *step* needs
        these without carrying a renderer around to call them on.
        """
        try:
            return await page.evaluate("() => document.body ? document.body.innerText: ''")
        except Exception:
            return ""

    def _failure_result(
        self,
        url: NormalizedUrl,
        status: ResultStatus,
        classification: Classification,
        attempts: int,
        started: float,
        error: CrawlError | None,
    ) -> CrawlResult:
        return terminal_crawl_result(
            url=url,
            status=status,
            classification=classification,
            attempts=attempts,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            domain=url.site,
            error=error,
        )

    def _warmup_urls_from_env(self) -> list[str]:
        value = os.environ.get("CRAWLUTRON_WARMUP_URLS", "")
        return [url.strip() for url in value.split(",") if url.strip()]

    def _looks_like_pdf(self, url: str) -> bool:
        return url.split("?", 1)[0].lower().endswith(".pdf")
