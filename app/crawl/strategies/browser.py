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
import contextlib
import json
import logging
import os
import threading
import time
from collections import OrderedDict
from collections.abc import (
    Awaitable,
    Callable,
    Iterable,
    Iterator,
    MutableMapping,
)
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from _typeshed import SupportsKeysAndGetItem

from camoufox.async_api import AsyncCamoufox

from app.classify import classify_page
from app.constants import (
    DEFAULT_CHALLENGE_WAIT_SECONDS,
    DEFAULT_COOKIE_HARVEST_TIMEOUT_SECONDS,
    DEFAULT_MAX_BROWSER_RENDERS,
    DEFAULT_MAX_CONCURRENT_PAGES,
    status_for_classification,
)
from app.constants import (
    is_analytics_host as _is_analytics_url,
)
from app.crawl.result_helpers import _classify_error_code, _terminal_crawl_result
from app.crawl.strategies.browser_actions import (
    LAZY_SCROLL_DELAY_SECONDS,
    LAZY_SCROLL_STEPS,
    BrowserActionsMixin,
)
from app.crawl.strategies.document import _default_document_text_extractor
from app.crawl.strategies.pinned_transport import (
    _build_pinned_request,
    _build_pinned_request_async,
    check_ssrf_protection,
)
from app.json_safe import json_default
from app.models import Classification, CrawlError, CrawlResult, RenderOptions, ResultStatus
from app.safe_url import SafeUrl
from app.settings import CrawlerSettings
from app.validation import (
    HostPolicy,
    get_env_float,
    get_env_int,
    normalize_public_http_url,
    parse_bool_value,
    registrable_domain,
)

logger = logging.getLogger(__name__)


def _refuse_storage_state_without_cache_dir(storage_state: str) -> None:
    """Log why a ``storage_state`` was dropped because no ``cache_dir`` is set.

    The caller owns the return value; this owns the explanation. A separate
    chokepoint for one reason: this is the only refusal in the validator that is
    not "that path is not allowed". The configured-``cache_dir`` rejections stay
    silent, because there the allowlist exists and the answer is simply no. With
    no ``cache_dir`` no allowlist was ever configured, and that is the actionable
    fact -- so this one names the setting instead of letting the caller return a
    bare ``None`` indistinguishable from a path merely outside the allowlist.

    The allowlist this replaced was the platform temp dir, which is not operator
    control: it is world-readable on most hosts, so any local user could seed a
    file another tenant's render then loads as its session. Losing a local dev's
    ``storage_state`` is the cost; that is a diagnostic and a config line, against
    a deployment's session otherwise.
    """
    logger.warning(
        "browser_storage_state_no_cache_dir",
        extra={
            "storage_state": str(storage_state),
            "detail": (
                "no cache_dir is configured, so there is no allowlist to check this path "
                "against and the storage_state is dropped; set CRAWLUTRON_CACHE_DIR to a "
                "directory this deployment owns and place the file there"
            ),
        },
    )
    return None


async def _resolve_browser_target(url: str, *, policy: HostPolicy) -> tuple[str, dict[str, str]]:
    """SSRF-check + IP-pin ``url`` for browser navigation.

    Returns ``(pinned_url, extra_http_headers)``. The browser is told to connect
    to the single resolved IP (preventing DNS-rebinding) while preserving the
    original ``Host`` header. Raises ``ValueError`` if the target is unsafe.

    Async because pinning resolves DNS, and a render is not the only thing on the
    loop: called synchronously this ran ``socket.getaddrinfo`` inline, so every
    other in-flight crawl waited out the resolver once per rendered page. The
    off-loop entry is the one derived from the pin rule, so this cannot acquire a
    second, subtly different way of pinning.
    """
    pinned_url, request_headers = await _build_pinned_request_async(url, {}, policy=policy)
    host = request_headers.get("Host")
    return pinned_url, ({"Host": host} if host else {})


def _assert_safe_final_url(final_url: str, *, policy: HostPolicy) -> None:
    """Re-validate the post-navigation URL.

    The rendered site may have redirected the browser to an internal host. Fail
    closed (raise ``ValueError``) rather than returning potentially-internal
    content.
    """
    normalized = normalize_public_http_url(final_url, policy=policy)
    check_ssrf_protection(normalized)


def _is_ssrf_blocked(url: str, *, policy: HostPolicy) -> bool:
    """Return ``True`` if ``url`` resolves to an SSRF-unsafe target."""
    try:
        normalized = normalize_public_http_url(url, policy=policy)
        check_ssrf_protection(normalized)
    except ValueError:
        return True
    return False


# Subresource types that carry no crawlable text content. When the
# browser profile runs in "reduced" resource mode, these are aborted at the
# route layer to cut render bandwidth; the crawler only needs
# document/script/xhr/stylesheet to extract content.
# Tracking/analytics hosts: never fetch them -- they return no crawlable
# content and only add network load. The canonical list lives in
# app.constants (single source of truth) so the engine can also refuse to
# enqueue these hosts as jobs.
_REDUCED_BLOCK_RESOURCE_TYPES = frozenset({"image", "font", "media"})


def _make_ssrf_route_handler(
    resource_mode: str | None = None,
    *,
    include_screenshot: bool = False,
    policy: HostPolicy,
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
    same cached check the websocket handler uses: ``SafeUrl.connect`` keeps
    the original hostname there (real certificates carry no IP address SAN,
    so a pin would only break SNI/cert verification), making an uncached pin
    resolve per subresource pure cost with no security gain. HTTPS stays
    blocklist-checked before the browser connects.
    """

    async def _handler(route, request):
        # Re-validate and pin the IP for this specific subresource request
        # to prevent DNS-rebinding TOCTOU. _build_pinned_request
        # raises ValueError if the target resolves to a private/loopback IP,
        # and returns (pinned_url, {Host: original_hostname}) so we can
        # force the browser to connect to the validated IP while preserving
        # the Host header (virtual-host routing).
        request_scheme = request.url.split(":", 1)[0].lower()
        if request_scheme not in ("http", "https", ""):
            # Data:, blob:, about:, javascript: never touch the network via DNS;
            # let the browser handle them locally instead of aborting.
            await route.continue_()
            return
        # Tracking/analytics requests carry no crawlable content; drop them
        # unconditionally so they never consume bandwidth or trigger their own
        # SSRF resolution.
        if _is_analytics_url(request.url):
            await route.abort()
            return
        # Abort non-essential presentation subresources unconditionally: a
        # text-extraction crawler never needs images/fonts/media, and blocking
        # them cuts ~70% of rendered-page bandwidth. They are only permitted
        # when an explicit screenshot is requested.
        if getattr(request, "resource_type", "") in _REDUCED_BLOCK_RESOURCE_TYPES and not include_screenshot:
            await route.abort()
            return
        try:
            # Annotated once, ahead of both branches: the https branch assigns
            # an empty literal, which mypy cannot widen to the dict type the
            # http branch's _build_pinned_request returns.
            host_headers: dict[str, str]
            if request_scheme == "https":
                # The pinned IP is discarded for https (SafeUrl.connect keeps
                # the original hostname so TLS SNI and certificate validation
                # work -- real certs carry no IP SAN), so the pin resolve is
                # discarded work here. Validate through the cached, TTL'd
                # check instead: one resolver round trip per host, not one
                # uncached resolve per subresource. The check still fails
                # closed on private/loopback/resolved-internal targets.
                await asyncio.to_thread(
                    SafeUrl.parse,
                    request.url,
                    policy=policy,
                )
                pinned_url, host_headers = request.url, {}
            else:
                pinned_url, host_headers = await asyncio.to_thread(
                    _build_pinned_request,
                    request.url,
                    {},
                    policy=policy,
                )
        except Exception:
            # Fail closed: DNS timeout, resolver errors, anything unexpected ->
            # abort rather than let an unchecked connection proceed.
            await route.abort()
            return
        overrides = {}
        if host_headers.get("Host"):
            overrides["Host"] = host_headers["Host"]
        # Omit headers= when there are no overrides: Playwright treats
        # an explicit headers dict as a full replacement, so passing {}
        # would wipe UA/cookies/accept headers off every HTTPS subresource.
        if overrides:
            await route.continue_(url=pinned_url, headers=overrides)
        else:
            await route.continue_(url=pinned_url)

    return _handler


def _make_ssrf_websocket_handler(*, policy: HostPolicy):
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
                normalize_public_http_url,
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


# The blocked/failed label groups live in app.constants now, alongside the
# classification -> status table both strategies share, so the HTTP and browser
# paths cannot disagree about a label again. Importers use the constants
# directly, exactly like the LAZY_SCROLL_* aliases below.

# Canonical defaults live in app.constants (single source of truth); these
# module names stay as the settings-less fallback so existing references
# (including the test-suite scroll disabler) keep working with identical values.
_BROWSER_COOKIES_MAX = 500


class _LockedDomainCookieStore(MutableMapping[str, str]):
    """LRU cookie store whose every read and mutation is internally guarded.

    The store is module-global and genuinely concurrent: the render path writes
    harvested cookies while the crawl engine reads them for the cffi
    cookie-bootstrap retry, from different tasks and threads. It used to be a
    bare ``OrderedDict``, and the *read* path mutated it (``get`` then
    ``move_to_end``): a concurrent write past the cap could evict the key in
    between and make the touch raise ``KeyError`` on a live request.

    The mapping is private and every operation acquires ``_lock`` for its whole
    duration, so an unsynchronized access is not a state a caller can construct
    -- the eviction can no longer interleave with a read.

    Behaves as a read/write mapping (``len``, ``iter``, ``dict()``, ``pop``,
    ``update``, ``clear``) for tests and teardown helpers; production traffic
    goes through :func:`_store_browser_cookies` /
    :func:`get_browser_cookies_for`, which own the LRU policy.
    """

    __slots__ = ("_data", "_lock", "_max_entries")

    def __init__(self, max_entries: int) -> None:
        # OrderedDict: insertion order doubles as the eviction order (plain
        # dict has no move_to_end/popitem(last=False)).
        self._data: OrderedDict[str, str] = OrderedDict()
        self._lock = threading.Lock()
        self._max_entries = max_entries

    # -- production surface -------------------------------------------------

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

    # -- mapping primitives (each one guarded) ------------------------------

    def __getitem__(self, domain: str) -> str:
        with self._lock:
            return self._data[domain]

    def __setitem__(self, domain: str, header: str) -> None:
        with self._lock:
            self._data[domain] = header

    def __delitem__(self, domain: str) -> None:
        with self._lock:
            del self._data[domain]

    def __iter__(self) -> Iterator[str]:
        with self._lock:
            return iter(list(self._data))

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def __contains__(self, domain: object) -> bool:
        with self._lock:
            return domain in self._data

    # ``MutableMapping`` derives these from the primitives above, which would
    # take the lock once per step; single-acquisition versions keep a snapshot
    # helper (test teardown, reset paths) from observing a half-applied op.

    def pop(self, domain: str, *default: Any) -> Any:
        with self._lock:
            return self._data.pop(domain, *default)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def update(
        self,
        other: SupportsKeysAndGetItem[str, str] | Iterable[tuple[str, str]] = (),
        /,
        **kwargs: str,
    ) -> None:
        merged = dict(other)
        if kwargs:
            merged.update(kwargs)
        with self._lock:
            for domain, header in merged.items():
                self._data[domain] = header

    def __repr__(self) -> str:
        with self._lock:
            return f"{type(self).__name__}({dict(self._data)!r})"


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


class BrowserRenderer(BrowserActionsMixin):
    def __init__(
        self,
        *,
        browser_factory: Callable[[], Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        challenge_wait_seconds: float | None = None,
        pdf_text_extractor: Callable[[str, HostPolicy], str] | None = None,
        warmup_urls: list[str] | None = None,
        settings: CrawlerSettings | None = None,
    ) -> None:
        self._browser_factory = browser_factory or self._create_camoufox_browser
        self._sleep = sleep
        # Prefer the passed policy limit, then the centralized settings field,
        # then the raw env var (falling back to the canonical default). This
        # keeps the default in one place (app.constants) instead of a bare
        # literal here.
        if challenge_wait_seconds is None:
            if settings is not None:
                self._challenge_wait_seconds = float(settings.challenge_wait_seconds)
            else:
                self._challenge_wait_seconds = get_env_float(
                    "CRAWLUTRON_CHALLENGE_WAIT_SECONDS", DEFAULT_CHALLENGE_WAIT_SECONDS, lo=0.0, hi=300.0
                )
        else:
            self._challenge_wait_seconds = challenge_wait_seconds
        # Fixed-overhead knobs (configurability only):
        # operator settings when present, else the module defaults above
        # (identical values). Per-request browser_profile keys override both
        # at render time via _scroll_config_for/_harvest_timeout_for.
        self._lazy_scroll_steps = (
            settings.browser_lazy_scroll_steps if settings is not None else LAZY_SCROLL_STEPS
        )
        self._lazy_scroll_delay_seconds = (
            settings.browser_lazy_scroll_delay_seconds if settings is not None else LAZY_SCROLL_DELAY_SECONDS
        )
        self._harvest_timeout_seconds = (
            settings.browser_cookie_harvest_timeout_seconds
            if settings is not None
            else DEFAULT_COOKIE_HARVEST_TIMEOUT_SECONDS
        )
        self._pdf_text_extractor = pdf_text_extractor or _default_document_text_extractor
        self._warmup_urls = warmup_urls if warmup_urls is not None else self._warmup_urls_from_env()
        # Reusable browser instance: one browser per BrowserRenderer lifecycle.
        self._browser_cm: Any = None
        # Cache dir for safe storage_state validation. Defaults to /tmp if no
        # settings were passed (single-tenant local-mode usage), but when
        # settings are available the operator-controlled cache_dir is used so
        # a malicious storage_state path cannot piggy-back on the world-
        # readable /tmp.
        self._cache_dir = settings.cache_dir if settings is not None else None
        self._browser: Any = None
        self._browser_reused = False
        # Recycle the reused browser after this many successful renders to bound
        # the memory of a long-lived Camoufox process (see _acquire_browser).
        self._render_count = 0
        self._max_browser_renders = (
            settings.max_browser_renders
            if settings is not None
            else get_env_int("CRAWLUTRON_MAX_BROWSER_RENDERS", DEFAULT_MAX_BROWSER_RENDERS, lo=1)
        )
        # Cap concurrent page renders: N simultaneous requests each open a tab,
        # which otherwise crashes/OOMs the browser under burst load.
        _concurrent_pages = (
            settings.max_concurrent_pages
            if settings is not None
            else get_env_int("CRAWLUTRON_MAX_CONCURRENT_PAGES", DEFAULT_MAX_CONCURRENT_PAGES, lo=1)
        )
        self._page_semaphore = asyncio.Semaphore(max(1, _concurrent_pages))

    @property
    def warmup_urls(self) -> list[str]:
        """Resolved warmup URLs for this renderer's browser profile.

        The headed-warmed strategy profile is the one carrying warmup URLs (see
        ``app.constants.BROWSER_STRATEGIES``); exposing the resolved list lets
        the browser pool label its renders with the real browser mode instead
        of inventing a strategy name on a failure path.
        """
        return self._warmup_urls

    async def _async_sleep(self, seconds: float) -> None:
        """Non-blocking sleep for use inside coroutines.

        The default ``time.sleep`` clock (injectable for tests) would block the
        whole event loop; run it in a thread so one render can never freeze the
        service.
        """
        if seconds <= 0:
            return
        await asyncio.to_thread(self._sleep, seconds)

    async def _harvest_session_cookies(self, page: Any, url: str) -> None:
        """Capture the page's cookies into the per-domain bootstrap store.

        Must run BEFORE the page/context is closed -- Camoufox destroys the
        context with the last page, leaving ``browser.contexts`` empty -- and
        must COMPLETE before returning: the cffi retry reads the store
        synchronously right after the render, so a fire-and-forget task would
        usually lose the race and retry cookieless. Harvest failures never
        break the render itself.
        """
        try:
            from app.validation import registrable_domain as _rd

            context = getattr(page, "context", None)
            if context is None:
                return
            cookies = await context.cookies(url)
            header = "; ".join(f"{c['name']}={c['value']}" for c in cookies if c.get("name"))
            if header:
                _store_browser_cookies(_rd(url), header)
                logger.info("browser_cookies_harvested", extra={"domain": _rd(url), "count": len(cookies)})
        except Exception as error:
            logger.debug("browser_cookies_harvest_failed", extra={"error": str(error)})

    async def _release_browser(self) -> None:
        """Take the cached context manager and tear it down, best-effort.

        The single owner of "forget the browser": both the per-attempt reset
        and the service-shutdown close have to drop the same three fields, and
        when they were spelled separately one of them grew a stale
        ``_browser_reused`` that the other did not clear. Clearing the whole
        set here means a field added later is cleared in both places or in
        neither, which is the only way that stays true.
        """
        cm = self._browser_cm
        self._browser_cm = None
        self._browser = None
        self._browser_reused = False
        if cm is not None:
            with contextlib.suppress(Exception):  # best-effort teardown
                await cm.__aexit__(None, None, None)

    async def render(
        self,
        url: str,
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
                last_error = _classify_error_code(error)
                await self._release_browser()
                if attempts >= max_attempts:
                    return self._failure_result(
                        url, "retry_exhausted", classification, attempts, started, last_error
                    )
                await self._async_sleep(min(2 ** (attempts - 1), 5))

        return self._failure_result(url, "failed", "unknown_error", attempts, started, last_error)

    async def _acquire_browser(self, options: RenderOptions) -> Any:
        """Return a reused browser instance, launching one on first use.

        Recycles the browser once it has served ``_max_browser_renders`` to
        bound memory growth of a long-lived Camoufox process.
        """
        if (
            self._browser is None
            or getattr(self._browser, "is_closed", False)
            or self._render_count >= self._max_browser_renders
        ):
            if self._browser is not None:
                await self._release_browser()
            launch_kwargs = self._get_browser_kwargs(options.browser_profile)
            cm = self._browser_factory(**launch_kwargs)
            self._browser = await cm.__aenter__()
            self._browser_cm = cm
            self._browser_reused = False
            self._render_count = 0
        else:
            self._browser_reused = True
        return self._browser

    @staticmethod
    async def _safe_close_page(page: Any) -> None:
        """Best-effort page teardown so rendered tabs/processes are not leaked."""
        if page is None:
            return
        with contextlib.suppress(Exception):  # best-effort teardown
            close = getattr(page, "close", None)
            if close is not None:
                await close()

    async def close(self) -> None:
        """Release the reused browser instance at service shutdown."""
        await self._release_browser()

    async def _render_with_browser(
        self,
        url: str,
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
        await self._wait_for_ready_state(page)

    async def _render_with_page_unlocked(
        self,
        url: str,
        options: RenderOptions,
        *,
        policy: HostPolicy,
    ) -> CrawlResult:
        # SSRF protection: validate + IP-pin the target so the browser
        # connects to a single resolved IP (DNS-rebinding safe) while preserving
        # the original Host header. Raises ValueError if the target is unsafe.
        pinned_url, goto_headers = await _resolve_browser_target(url, policy=policy)

        started = time.monotonic()

        browser = await self._acquire_browser(options)
        page = await browser.new_page(**self._get_page_kwargs(options.browser_profile))
        # Abort any request (navigation, redirect, or subresource) that
        # resolves to an SSRF-unsafe host before the browser connects.
        await page.route(
            "**/*",
            _make_ssrf_route_handler(
                (options.browser_profile or {}).get("resource_mode"),
                include_screenshot=bool((options.browser_profile or {}).get("include_screenshot")),
                policy=policy,
            ),
        )
        ws_guard = _make_ssrf_websocket_handler(policy=policy)
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
            page.on("response", self._make_response_handler(intercepted_xhr))

            # Warmup URLs (no Host header needed)
            for warmup_url in self._warmup_urls:
                normalized_warmup = normalize_public_http_url(warmup_url, policy=policy)
                await page.goto(normalized_warmup, wait_until="domcontentloaded")
                await self._wait_for_ready_state(page)

            # Navigate to target URL: set Host header for pinned IP navigation.
            # page.goto() does not accept extra_http_headers; use set_extra_http_headers().
            # The navigation honors RenderOptions.timeout_seconds via
            # wait_for: expiration raises TimeoutError, which the render
            # retry envelope maps to navigation_timeout. Floor at 1s so a
            # zero/negative budget cannot break every render.
            navigation_timeout = max(1.0, float(options.timeout_seconds))
            if goto_headers:
                await page.set_extra_http_headers(goto_headers)
            await asyncio.wait_for(
                self._navigate_and_settle(page, pinned_url),
                timeout=navigation_timeout,
            )
            if goto_headers:
                # Clear the page-scoped Host override: it was only needed for
                # the pinned navigation. Leaving it set would spoof Host on
                # every later request from this page (third-party XHR,
                # subsequent navigations).
                await page.set_extra_http_headers({})

            # Browser action chains: run policy-specified interactions before extraction.
            actions = (options.browser_profile or {}).get("actions") or []
            if actions:
                await self._run_action_chain(page, actions, url=url, budget_seconds=navigation_timeout)

            # Lazy-load scroll: trigger deferred/infinite-scroll content.
            await self._lazy_load_scroll(page, options.browser_profile)

            # Shadow-DOM flattening: inline open shadow roots so their links
            # and visible text are reachable by the standard HTML/text extractors.
            await self._flatten_shadow_dom(page)

            # Auto-accept cookie consent (opt-in only) before extraction
            await self._auto_accept_cookie_consent(
                page, bool((options.browser_profile or {}).get("auto_accept_cookie_consent"))
            )

            # Extract with challenge waiting
            html, text, classification, challenge_resolved = await self._extract_with_challenge_wait(
                page, options
            )

            extraction_method = "dom_text"
            final_url = page.url
            # Re-validate the post-navigation URL: the site may have
            # redirected the browser to an internal host. Fail closed.
            _assert_safe_final_url(final_url, policy=policy)

            # PDF extraction fallback
            if self._looks_like_pdf(final_url) and not (text or "").strip():
                # Blocking file read + parse: keep it off the event loop like
                # every other document-extraction path (the extractor may be a
                # sync downloader/parser).
                text = await asyncio.to_thread(self._pdf_text_extractor, final_url, policy)
                html = html if options.include_html else None
                title = await page.evaluate("() => document.title")
                classification = classify_page(html, text, title=title)
                extraction_method = "pdf_text"

            status = status_for_classification(classification)

            metadata: dict[str, Any] = {
                "title": await page.evaluate("() => document.title"),
                "content_length": len(html or ""),
                "browser_reused": self._browser_reused,
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
                metadata["element_screenshots"] = await self._capture_element_screenshots(page, element_shots)

            return CrawlResult(
                url=url,
                status=status,
                classification=classification,
                final_url=final_url,
                html=html,
                text=text,
                attempts=1,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                domain=registrable_domain(url),
                metadata=metadata,
            )
        finally:
            # Always release the page so the rendered tab/process is not leaked
            # between crawls (the root of the Camoufox memory growth).
            # a wedged context.cookies() must not hold the pooled renderer
            # for the whole per-URL budget; fall back to cookieless (
            # behavior) after a short bounded wait.
            try:
                await asyncio.wait_for(
                    self._harvest_session_cookies(page, url),
                    timeout=self._harvest_timeout_for(options.browser_profile or {}),
                )
            except TimeoutError:
                pass
            await self._safe_close_page(page)
            # Increment render count for ALL attempts (success or failure) so
            # browser recycle logic bounds total attempts per browser instance.
            self._render_count += 1

    @staticmethod
    def _make_response_handler(collected: list[dict[str, Any]]) -> Callable[[Any], Awaitable[None]]:
        # Bounds on intercepted XHR: a hostile page can fire thousands of
        # JSON responses. Cap at 10 entries x 8KB payload (was 50 x 64KB =
        # ~3.2MB held per page in result metadata); feed/API discovery does not
        # need more than a small sample. The URL is page-controlled too and is
        # bounded on the same terms: an entry cap is not a bound on what an entry
        # may contain, and ten multi-megabyte request URLs is ten megabytes of
        # attacker-chosen text reaching result metadata through a field the entry
        # count appeared to cover.
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
                # ACCEPTED RESIDUAL: the browser process has already
                # materialized this body in its own memory; Python parses it
                # once here (transiently) before the 8KB retention cap. Bounding
                # the parse itself would silently drop legitimate large JSON
                # API payloads from link discovery.
                payload = await response.json()
            except Exception:  # payload capture is best-effort
                payload = None
            if payload is not None and len(json.dumps(payload, default=json_default)) > max_payload_chars:
                entry["payload_truncated"] = True
            else:
                entry["payload"] = payload
            collected.append(entry)

        return on_response

    def _get_browser_kwargs(self, browser_profile: dict[str, Any]) -> dict[str, Any]:
        """Launch-only kwargs for the Camoufox/Playwright browser factory.

        Persistent cookies are passed via ``storage_state`` so cookies from a
        previous session are retained across recursive page loads."""
        kwargs: dict[str, Any] = {
            "headless": self._headless_enabled(),
            "humanize": True,
        }

        profile = browser_profile or {}

        if profile.get("resource_mode") == "reduced":
            kwargs["args"] = ["--blink-settings=imagesEnabled=false"]

        storage_state = profile.get("storage_state")
        if storage_state:
            # Validate storage_state path to prevent path traversal
            safe_path = self._validate_storage_state_path(storage_state)
            if safe_path:
                kwargs["storage_state"] = safe_path
            else:
                logger.warning("invalid_storage_state_path", extra={"path": str(storage_state)})

        return kwargs

    def _validate_storage_state_path(self, storage_state: str) -> str | None:
        """Validate storage_state path is safe (no traversal, under allowed dirs).

        Returns a path that is safe to hand to Playwright, or None.

        Every return value is a real path under an operator-controlled
        directory. Playwright types ``storage_state`` as ``Union[str,
        pathlib.Path] | Dict`` -- "can be a path to a file or a dictionary" --
        so a string here is ALWAYS read as a file path and there is no
        named-session form to support. This used to wave through the literals
        "default", "session" and "persistent" as if they named sessions, which
        made the chokepoint that exists to stop a caller reaching a path it
        should not instead hand out three unvalidated paths relative to the
        process working directory.

        The allowlist is exactly the operator's ``cache_dir`` and nothing else.
        The platform temp dir is NOT an implicit root: it is world-readable on
        most hosts, so treating it as trusted would let any local user seed a
        file another tenant's render then loads as its session. See
        :func:`_refuse_storage_state_without_cache_dir` for the no-``cache_dir``
        case, which fails closed rather than substituting a guess.
        """
        from pathlib import Path

        # For file paths, ensure they live under an operator-controlled dir.
        with contextlib.suppress(Exception):
            path = Path(storage_state).resolve()
            if not self._cache_dir:
                _refuse_storage_state_without_cache_dir(storage_state)
                return None
            path.relative_to(Path(self._cache_dir).resolve())
            return str(path)
        return None

    def _get_page_kwargs(self, browser_profile: dict[str, Any]) -> dict[str, Any]:
        """Page/context kwargs, applied at ``browser.new_page`` time."""
        profile = browser_profile or {}
        viewport = profile.get("viewport")
        if viewport == "mobile":
            return {"viewport": {"width": 390, "height": 844}}
        return {"viewport": {"width": 1365, "height": 768}}

    async def _extract_with_challenge_wait(
        self, page: Any, options: RenderOptions
    ) -> tuple[str | None, str | None, Classification, bool]:
        html = await self.extract_html(page) if options.include_html else None
        text = await self._extract_text(page) if options.include_text else None
        title = await page.evaluate("() => document.title")
        classification = classify_page(html, text, title=title)

        if classification != "bot_challenge_detected":
            return html, text, classification, False

        deadline = time.monotonic() + self._challenge_wait_seconds
        while time.monotonic() < deadline:
            # Use asyncio.sleep to avoid busy-waiting if injected sleep is patched/no-op
            await asyncio.sleep(1)
            await self._wait_for_ready_state(page)
            next_html = await self.extract_html(page) if options.include_html else None
            next_text = await self._extract_text(page) if options.include_text else None
            next_title = await page.evaluate("() => document.title")
            next_classification = classify_page(next_html, next_text, title=next_title)
            if next_classification != "bot_challenge_detected":
                return next_html, next_text, next_classification, True
            html, text, classification = next_html, next_text, next_classification

        return html, text, classification, False

    async def _wait_for_ready_state(self, page: Any) -> None:
        for _ in range(10):
            state = await page.evaluate("() => document.readyState")
            if state in {"interactive", "complete"}:
                return
            await self._async_sleep(0.1)

    async def extract_html(self, page: Any) -> str:
        try:
            return await page.content()
        except Exception:
            return ""

    async def _extract_text(self, page: Any) -> str:
        try:
            return await page.evaluate("() => document.body ? document.body.innerText: ''")
        except Exception:
            return ""

    async def _flatten_shadow_dom(self, page: Any) -> None:
        # Move the children of every open shadow root into its light-DOM host
        # so that page.content() (and thus link/text extraction) includes shadow-DOM
        # content. Closed shadow roots are not accessible and are left untouched.
        # Nested shadow roots are handled by recursing into each shadow root before
        # its children are moved.
        # FIX: Collect shadow root children FIRST, then move in separate pass to
        # avoid mutation during iteration (could cause duplicates, infinite loops,
        # or HierarchyRequestError if moving a node into its own descendant).
        with contextlib.suppress(Exception):  # flattening is best-effort; never fail a render over it
            await page.evaluate(
                """() => {
                    const collect = (root, acc) => {
                        const els = root.querySelectorAll ? root.querySelectorAll('*'): [];
                        for (const el of els) {
                            acc.push(el);
                            if (el.shadowRoot && el.shadowRoot.mode === 'open') {
                                collect(el.shadowRoot, acc);
                            }
                        }
                    };
                    const all = [];
                    collect(document, all);
                    // Phase 1: Collect all (host, child) pairs to move
                    const moves = [];
                    for (const el of all) {
                        const sr = el.shadowRoot;
                        if (sr && sr.mode === 'open') {
                            const kids = Array.from(sr.childNodes);
                            for (const k of kids) {
                                if (k.nodeType === 1) {
                                    // Deep-clone to avoid mutation issues
                                    moves.push({host: el, child: document.importNode(k, true)});
                                }
                            }
                        }
                    }
                    // Phase 2: Apply all moves
                    for (const {host, child} of moves) {
                        try {
                            host.appendChild(child);
                        } catch (e) {
                            // Ignore HierarchyRequestError etc. (best-effort)
                        }
                    }
                    return true;
                }"""
            )

    async def _auto_accept_cookie_consent(self, page: Any, enabled: bool) -> bool:
        """Auto-click cookie consent accept buttons (best-effort, opt-in).

        Clicking "Accept All" on the caller's behalf is an action with legal
        weight, so it never happens implicitly: callers must pass
        ``browser_profile.auto_accept_cookie_consent = true``. Returns True if
        a consent button was clicked, False otherwise.
        """
        if not enabled:
            return False
        try:
            # Common cookie consent button selectors and text patterns
            consent_selectors = [
                # Generic button/anchor with common accept text
                'button:has-text("Accept All")',
                'button:has-text("Accept all")',
                'button:has-text("Accept All Cookies")',
                'button:has-text("Accept all cookies")',
                'button:has-text("I Accept")',
                'button:has-text("I Agree")',
                'button:has-text("Allow All")',
                'button:has-text("Allow all")',
                'button:has-text("Continue")',
                'button:has-text("Got it")',
                'button:has-text("OK")',
                'button:has-text("Ok")',
                'a:has-text("Accept All")',
                'a:has-text("Accept all")',
                'a:has-text("I Accept")',
                'a:has-text("I Agree")',
                # Common ID/class patterns
                'button[id*="accept"]',
                'button[class*="accept"]',
                'button[id*="consent"]',
                'button[class*="consent"]',
                'button[id*="cookie"]',
                'button[class*="cookie"]',
                "#onetrust-accept-btn-handler",
                ".cc-btn.cc-allow",
                ".cookie-consent-accept",
                '[data-testid="cookie-accept"]',
                '[data-cy="accept-cookies"]',
            ]

            for selector in consent_selectors:
                with contextlib.suppress(Exception):
                    element = await page.query_selector(selector)
                    if element:
                        # Check if element is visible
                        is_visible = await element.is_visible()
                        if is_visible:
                            await element.click()
                            await self._async_sleep(0.5)  # Brief wait for consent to register
                            return True
            # Fallback: evaluate JS to find buttons by text content
            clicked = await page.evaluate("""() => {
                const acceptTexts = [
                    'accept all', 'accept all cookies', 'i accept', 'i agree',
                    'allow all', 'allow all cookies', 'continue', 'got it', 'ok'
                ];
                const buttons = document.querySelectorAll('button, a, input[type="button"],
                input[type="submit"]');
                for (const btn of buttons) {
                    const text = (btn.innerText || btn.value || btn.textContent || '').toLowerCase().trim();
                    if (acceptTexts.some(t => text.includes(t))) {
                        // Check if visible
                        const style = window.getComputedStyle(btn);
                        if (style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !==
                        '0') {
                            btn.click();
                            return true;
                        }
                    }
                }
                return false;
            }""")
            return bool(clicked)
        except Exception:
            return False

    def _failure_result(
        self,
        url: str,
        status: ResultStatus,
        classification: Classification,
        attempts: int,
        started: float,
        error: CrawlError | None,
    ) -> CrawlResult:
        return _terminal_crawl_result(
            url=url,
            status=status,
            classification=classification,
            attempts=attempts,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            domain=registrable_domain(url),
            error=error,
        )

    def _create_camoufox_browser(self, **kwargs: Any):
        """Factory for AsyncCamoufox browser."""
        return AsyncCamoufox(**kwargs)

    def _warmup_urls_from_env(self) -> list[str]:
        value = os.environ.get("CRAWLUTRON_WARMUP_URLS", "")
        return [url.strip() for url in value.split(",") if url.strip()]

    def _headless_enabled(self) -> bool:
        return parse_bool_value(
            os.environ.get("CRAWLUTRON_HEADLESS"),
            default=True,
            env_name="CRAWLUTRON_HEADLESS",
        )

    def _looks_like_pdf(self, url: str) -> bool:
        return url.split("?", 1)[0].lower().endswith(".pdf")
