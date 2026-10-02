"""The browser, and the page work a render performs, in one substitutable place.

The seam is a boundary, not a file split. Everything here touches a LIVE browser
or a live page: launching it, recycling it, opening a tab, and doing page work
(ready-state, action chains, lazy scroll, shadow-DOM flattening, cookie consent,
teardown). :class:`~app.crawl.strategies.browser.BrowserRenderer` keeps the other
half -- what a render IS: navigate, extract, classify, screenshot, fail.

The boundary exists because the two halves are substituted for different reasons.
A render is driven by a caller, so it is worth faking in a test; a real browser
is not, so it was reached only through the renderer's own private methods.
``build_pinned_renderer`` rebound ten of them on the class to manufacture a seam
the type never offered -- and no AST gate can see that, because the target is the
class rather than a name.

``BrowserRenderer`` takes ``driver=``. The default builds the real one from the
settings it always read; a test passes a driver whose page steps do nothing and
whose ``open_page`` returns the page it was built with. Browser recycling lives
here rather than in the renderer because it bounds the growth of a long-lived
browser process, which is what this class owns.

The action vocabulary is NOT here. It is ``app.constants``
(``BROWSER_ACTION_TYPES`` and friends), because ``CrawlPolicy`` reads the same
set at the model boundary and two hand-maintained lists that must agree is the
bug this module used to be able to cause.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from camoufox.async_api import AsyncCamoufox

from app.constants import (
    BROWSER_ACTION_TYPES,
    DEFAULT_COOKIE_HARVEST_TIMEOUT_SECONDS,
    DEFAULT_LAZY_SCROLL_DELAY_SECONDS,
    DEFAULT_LAZY_SCROLL_STEPS,
    DEFAULT_MAX_BROWSER_RENDERS,
)
from app.validation import get_env_int, parse_bool_value

if TYPE_CHECKING:
    from app.settings import CrawlerSettings

logger = logging.getLogger(__name__)


def _profile_int(profile: dict[str, Any], key: str, default: int, ceiling: int | None = None) -> int:
    """Read a non-negative int knob from a browser_profile (never raises).

    ``ceiling`` clamps a caller-supplied value on the way through. Without one
    this is ``max(0, value)``, which is why ``lazy_scroll_steps: 10**12`` used to
    be honoured: a closed key set says which keys exist, and says nothing about
    how large their values may be. Clamping here rather than only at the model is
    the point -- by the time a profile reaches a renderer it is a dict, so the
    validator is no longer in the path.
    """
    try:
        value = int(profile.get(key, default))
    except (TypeError, ValueError):
        return default
    return _clamp_knob(max(0, value), ceiling)


def _profile_float(profile: dict[str, Any], key: str, default: float, ceiling: float | None = None) -> float:
    """Read a non-negative float knob from a browser_profile (never raises).

    The float sibling of :func:`_profile_int`, and bounded for the same reason:
    ``cookie_harvest_timeout_seconds`` became the ``wait_for`` timeout holding a
    pooled renderer, with nothing above it but a wall-clock budget.
    """
    try:
        value = float(profile.get(key, default))
    except (TypeError, ValueError):
        return default
    if value != value:  # NaN: keep the default rather than poisoning waits
        return default
    return _clamp_knob(max(0.0, value), ceiling)


#: The numeric knobs a browser profile carries. Constrained rather than bound to
#: ``float`` so ``_clamp_knob`` returns an ``int`` for the int readers: a
#: ``float`` return would quietly widen every scroll step through a float.
_Knob = TypeVar("_Knob", int, float)


def _clamp_knob[Knob: (int, float)](value: Knob, ceiling: Knob | None) -> Knob:
    """Hold a knob at its ceiling when it has one, and pass it through when it does not."""
    return value if ceiling is None else min(value, ceiling)


LAZY_SCROLL_STEPS = DEFAULT_LAZY_SCROLL_STEPS


LAZY_SCROLL_DELAY_SECONDS = DEFAULT_LAZY_SCROLL_DELAY_SECONDS


# Server-side clamp for caller-supplied action wait durations: a hostile
# policy must not be able to park a render (or, pre-fix, the event loop)
# for an arbitrary time.
MAX_ACTION_WAIT_SECONDS = 10.0


def _action_wait_ms(action: dict[str, Any]) -> int:
    """Clamp a caller-supplied ``wait`` timeout the way ``wait_seconds`` is.

    Unbounded values park the render for as long as the caller likes, and 0
    disables Playwright's timeout entirely instead of meaning "immediately".
    Garbage falls back to the 5s default rather than failing the action.
    """
    try:
        requested_ms = int(action.get("timeout", 5000))
    except (TypeError, ValueError):
        requested_ms = 5000
    return max(1, min(requested_ms, int(MAX_ACTION_WAIT_SECONDS * 1000)))


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


async def async_sleep(sleep: Callable[[float], None], seconds: float) -> None:
    """Sleep without blocking the event loop, through an injected clock.

    Both halves of a render need to wait -- the renderer between retries, the
    driver between scroll steps and action waits -- and the wait must never
    freeze the loop a render is running on. One implementation, two callers:
    duplicated here it would be two places to keep honest about ``to_thread``.
    """
    if seconds <= 0:
        return
    await asyncio.to_thread(sleep, seconds)


class BrowserDriver:
    """Owns the browser and does page work; the renderer decides what to ask for.

    ``browser_factory`` is the Camoufox/Playwright launcher, ``max_browser_renders``
    the recycle bound that keeps a long-lived browser process from growing without
    limit, and ``cache_dir`` the allowlist a caller-supplied ``storage_state`` must
    live inside. All three default from settings, so the renderer can build one
    without restating them.
    """

    def __init__(
        self,
        *,
        browser_factory: Callable[[], Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_browser_renders: int | None = None,
        cache_dir: str | Path | None = None,
        lazy_scroll_steps: int = LAZY_SCROLL_STEPS,
        lazy_scroll_delay_seconds: float = LAZY_SCROLL_DELAY_SECONDS,
        harvest_timeout_seconds: float = DEFAULT_COOKIE_HARVEST_TIMEOUT_SECONDS,
        settings: CrawlerSettings | None = None,
    ) -> None:
        self._browser_factory = browser_factory or self._create_camoufox_browser
        self._sleep = sleep
        self._max_browser_renders = (
            max_browser_renders
            if max_browser_renders is not None
            else (
                settings.max_browser_renders
                if settings is not None
                else get_env_int("CRAWLUTRON_MAX_BROWSER_RENDERS", DEFAULT_MAX_BROWSER_RENDERS, lo=1)
            )
        )
        # A ``storage_state`` may only be honoured when the operator configured an
        # allowlist to honour it against. Defaulting to the platform temp dir used
        # to be that allowlist, and it is world-readable on most hosts.
        self._cache_dir = cache_dir if settings is None else settings.cache_dir

        self._lazy_scroll_steps = (
            lazy_scroll_steps if settings is None else int(settings.browser_lazy_scroll_steps)
        )
        self._lazy_scroll_delay_seconds = (
            lazy_scroll_delay_seconds
            if settings is None
            else float(settings.browser_lazy_scroll_delay_seconds)
        )
        self._harvest_timeout_seconds = (
            harvest_timeout_seconds
            if settings is None
            else float(settings.browser_cookie_harvest_timeout_seconds)
        )
        self._browser_cm: Any = None
        self._browser: Any = None
        self._browser_reused = False
        self._render_count = 0

    async def open_page(self, browser_profile: dict[str, Any] | None) -> Any:
        """Return a page on a (possibly reused) browser, with profile kwargs applied.

        One call rather than acquire-then-new_page, because a caller that wants a
        page has no reason to know the browser is recycled underneath it -- and a
        test substituting this driver should not have to supply a fake browser
        whose only job is to have a ``new_page``.
        """
        browser = await self._acquire_browser(browser_profile)
        return await browser.new_page(**self.page_kwargs(browser_profile))

    @property
    def reused(self) -> bool:
        """Whether the current page was opened on a browser already serving earlier renders."""
        return self._browser_reused

    @property
    def cache_dir(self) -> str | Path | None:
        """Where storage-state files are allowed to live, or None when unset.

        Public because it is a safety boundary, not plumbing: with no cache
        directory the driver accepts no storage state at all, and a caller (or a
        test) asking "is any path acceptable here?" is asking about the
        contract, not about an attribute.
        """
        return self._cache_dir

    @property
    def browser(self) -> Any:
        """The live browser, or None while the driver holds none.

        Public because releasing it is a driver contract: after ``close()`` the
        driver must be back to holding nothing, and a test asserting that should
        not have to read a private attribute to find out.
        """
        return self._browser

    def note_render(self) -> None:
        """Count one render attempt, successful or not, against the recycle bound."""
        self._render_count += 1

    async def close(self) -> None:
        """Drop the cached browser. Best-effort, and safe to call more than once."""
        await self._release_browser()

    def _create_camoufox_browser(self, **kwargs: Any):
        """Factory for AsyncCamoufox browser."""
        return AsyncCamoufox(**kwargs)

    def headless_enabled(self) -> bool:
        return parse_bool_value(
            os.environ.get("CRAWLUTRON_HEADLESS"),
            default=True,
            env_name="CRAWLUTRON_HEADLESS",
        )

    def validate_storage_state_path(self, storage_state: str) -> str | None:
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

    def browser_kwargs(self, browser_profile: dict[str, Any] | None) -> dict[str, Any]:
        """Launch-only kwargs for the Camoufox/Playwright browser factory.

        Persistent cookies are passed via ``storage_state`` so cookies from a
        previous session are retained across recursive page loads."""
        kwargs: dict[str, Any] = {
            "headless": self.headless_enabled(),
            "humanize": True,
        }

        profile = browser_profile or {}

        if profile.get("resource_mode") == "reduced":
            kwargs["args"] = ["--blink-settings=imagesEnabled=false"]

        storage_state = profile.get("storage_state")
        if storage_state:
            # Validate storage_state path to prevent path traversal
            safe_path = self.validate_storage_state_path(storage_state)
            if safe_path:
                kwargs["storage_state"] = safe_path
            else:
                logger.warning("invalid_storage_state_path", extra={"path": str(storage_state)})

        return kwargs

    def page_kwargs(self, browser_profile: dict[str, Any] | None) -> dict[str, Any]:
        """Page/context kwargs, applied at ``browser.new_page`` time."""
        profile = browser_profile or {}
        viewport = profile.get("viewport")
        if viewport == "mobile":
            return {"viewport": {"width": 390, "height": 844}}
        return {"viewport": {"width": 1365, "height": 768}}

    async def _acquire_browser(self, browser_profile: dict[str, Any] | None) -> Any:
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
            launch_kwargs = self.browser_kwargs(browser_profile)
            cm = self._browser_factory(**launch_kwargs)
            self._browser = await cm.__aenter__()
            self._browser_cm = cm
            self._browser_reused = False
            self._render_count = 0
        else:
            self._browser_reused = True
        return self._browser

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

    @staticmethod
    async def safe_close_page(page: Any) -> None:
        """Best-effort page teardown so rendered tabs/processes are not leaked."""
        if page is None:
            return
        with contextlib.suppress(Exception):  # best-effort teardown
            close = getattr(page, "close", None)
            if close is not None:
                await close()

    async def wait_for_ready_state(self, page: Any) -> None:
        for _ in range(10):
            state = await page.evaluate("() => document.readyState")
            if state in {"interactive", "complete"}:
                return
            await self._async_sleep(0.1)

    async def flatten_shadow_dom(self, page: Any) -> None:
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

    async def auto_accept_cookie_consent(self, page: Any, enabled: bool) -> bool:
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

    async def run_action_chain(
        self,
        page: Any,
        actions: list[dict[str, Any]],
        *,
        url: str = "",
        budget_seconds: float | None = None,
    ) -> None:
        """Run policy-specified actions under ONE deadline, then move on.

        The chain had no deadline at all -- only the navigation above it was
        bounded -- so N ``wait`` actions held the page semaphore (one slot of the
        pool that exists because concurrent tabs crash the shared process) for
        N x ``MAX_ACTION_WAIT_SECONDS``, and the exhaustion that followed said
        nothing about the chain still waiting. The budget is the per-URL number
        the navigation already got, so the render's worst case stops growing
        with the chain length.

        ``budget_seconds=None`` is the unbounded form, for driving the chain
        directly; production must pass the render budget, which
        ``test_the_action_chain_is_always_invoked_with_a_budget`` enforces
        tree-wide, since an optional deadline is only as good as that gate.
        """
        if not actions:
            return
        if budget_seconds is None:
            await self._perform_action_chain(page, actions, url=url)
            return
        try:
            await asyncio.wait_for(self._perform_action_chain(page, actions, url=url), timeout=budget_seconds)
        except TimeoutError:
            logger.warning(
                "browser_action_chain_budget_exhausted",
                extra={
                    "url": url,
                    "budget_seconds": budget_seconds,
                    "actions": len(actions),
                },
            )

    async def _perform_action_chain(self, page: Any, actions: list[dict[str, Any]], *, url: str) -> None:
        """Execute a sequence of browser actions before extraction.

        Supported action types: ``click``, ``fill``, ``hover``, ``scroll``
        (into an element), ``scroll_bottom``, ``wait`` (for a selector), and
        ``wait_seconds``.

        Arbitrary JavaScript (``execute_js``) is intentionally NOT supported:
        running caller-supplied scripts in the browser context is an RCE-class
        risk (cookie/localStorage theft, requests to internal services with no
        SSRF guard).

        Actions are best-effort -- one bad selector must not cost the page its
        extraction -- but not silent, since a ``wait`` for an element a SPA
        never grows is an invisible extraction-quality loss.

        The per-action dispatch is a separate coroutine so this function stays the
        loop-and-accounting shape and the vocabulary it recognises reads as one
        list, rather than interleaved with the try/except that keeps a bad
        selector from costing the page its extraction.
        """
        for action in actions:
            if not isinstance(action, dict):
                logger.warning("browser_action_skipped", extra={"reason": "action_not_an_object"})
                continue
            action_type = str(action.get("type", "")).lower()
            if action_type not in BROWSER_ACTION_TYPES:
                # The model boundary already refuses an unknown type, so reaching
                # this is a route that bypassed it. Logged rather than skipped in
                # silence: a caller whose action was dropped needs to know, and
                # the alternative is the page loading with nothing done.
                logger.warning(
                    "browser_action_skipped",
                    extra={"reason": "unknown_action_type", "action_type": action_type},
                )
                continue
            try:
                await self._dispatch_action(page, action, action_type)
            except Exception as error:  # action chains are best-effort, not silent
                logger.warning(
                    "browser_action_failed",
                    extra={
                        "url": url,
                        "action": action_type,
                        "selector": action.get("selector"),
                        "wait_ms": action.get("timeout"),
                        "error": str(error),
                    },
                )

    async def _dispatch_action(self, page: Any, action: dict[str, Any], action_type: str) -> None:
        """Perform ONE recognised action. Errors propagate to the caller's handler.

        Split out of the chain loop so the recognised vocabulary reads as one
        if/elif and the chain loop reads as accounting. ``execute_js`` is
        recognised and refused here as well as at the model, because this is the
        chokepoint a policy arriving by any other route still cannot run script
        through.
        """
        selector = action.get("selector")
        if action_type == "click" and selector:
            await page.click(selector)
        elif action_type == "fill" and selector:
            await page.fill(selector, str(action.get("value", "")))
        elif action_type == "hover" and selector:
            await page.hover(selector)
        elif action_type == "scroll" and selector:
            element = await page.query_selector(selector)
            if element is not None:
                await element.scroll_into_view_if_needed()
        elif action_type == "scroll_bottom":
            await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        elif action_type == "wait" and selector:
            await page.wait_for_selector(selector, timeout=_action_wait_ms(action))
        elif action_type == "wait_seconds":
            await self._async_sleep(min(float(action.get("seconds", 1)), MAX_ACTION_WAIT_SECONDS))
        elif action_type == "execute_js":
            # Refuse arbitrary JS execution (RCE-class risk). Callers must
            # not be able to run scripts in the browser context.
            return

    async def capture_element_screenshots(self, page: Any, selectors: list[str]) -> list[dict[str, Any]]:
        """Capture targeted element screenshots as base64."""
        shots: list[dict[str, Any]] = []
        for selector in selectors:
            with contextlib.suppress(Exception):  # best-effort per-element capture
                element = await page.query_selector(selector)
                if element is None:
                    continue
                data = base64.b64encode(await element.screenshot()).decode("ascii")
                shots.append({"selector": selector, "screenshot": data})
        return shots

    async def lazy_load_scroll(self, page: Any, browser_profile: dict[str, Any] | None = None) -> None:
        """Scroll to the bottom repeatedly to trigger lazy-loaded content."""
        steps, delay = self.scroll_config_for(browser_profile)
        for _ in range(steps):
            try:
                await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            except Exception:  # scrolling is best-effort
                return
            await self._async_sleep(delay)

    def harvest_timeout_for(self, browser_profile: dict[str, Any] | None) -> float:
        """Resolve the cookie-harvest wait bound: policy profile beats settings.

        Ceilinged because this is the ``wait_for`` timeout on a *pooled* renderer
        held from a finally block, so an unbounded value is a hold on a shared
        resource rather than on the caller's own budget.
        """
        from app.constants import MAX_COOKIE_HARVEST_TIMEOUT_SECONDS

        profile = browser_profile or {}
        return _profile_float(
            profile,
            "cookie_harvest_timeout_seconds",
            self._harvest_timeout_seconds,
            MAX_COOKIE_HARVEST_TIMEOUT_SECONDS,
        )

    def scroll_config_for(self, browser_profile: dict[str, Any] | None) -> tuple[int, float]:
        """Resolve lazy-scroll steps/delay: policy profile beats settings.

        Ceilinged for the same reason as the harvest, one step further out: this
        runs directly below the budgeted action chain, and 10**12 steps is a
        render that never returns.
        """
        from app.constants import MAX_LAZY_SCROLL_DELAY_SECONDS, MAX_LAZY_SCROLL_STEPS

        profile = browser_profile or {}
        return (
            _profile_int(profile, "lazy_scroll_steps", self._lazy_scroll_steps, MAX_LAZY_SCROLL_STEPS),
            _profile_float(
                profile,
                "lazy_scroll_delay_seconds",
                self._lazy_scroll_delay_seconds,
                MAX_LAZY_SCROLL_DELAY_SECONDS,
            ),
        )

    async def _async_sleep(self, seconds: float) -> None:
        """Wait between page steps, off the event loop. See :func:`async_sleep`."""
        await async_sleep(self._sleep, seconds)
