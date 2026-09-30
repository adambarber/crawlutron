"""The caller-supplied browser action chain, and the knobs that bound it.

Split out of ``app/crawl.strategies.browser``, where ``BrowserRenderer`` had
grown past the file-size ceiling. The seam is one concern rather than an arbitrary
cut: everything here is about CALLER-DIRECTED interaction with a loaded page --
what actions a caller may ask for, how long one may wait, how far the page is
scrolled to trigger lazy content, and what gets screenshotted. None of it is
about acquiring or releasing a browser, which is what remains in ``browser.py``.

``BrowserRenderer`` inherits :class:`BrowserActionsMixin`, so every method here
is reached exactly as before; the split moves where a method is defined and
nothing about who calls it.

The action vocabulary itself is NOT here. It is ``app.constants``
(``BROWSER_ACTION_TYPES`` and friends), because ``CrawlPolicy`` reads the same
set at the model boundary and two hand-maintained lists that must agree is the
bug this module's docstring used to be able to cause.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
from typing import Any, TypeVar

from app.constants import (
    BROWSER_ACTION_TYPES,
    DEFAULT_LAZY_SCROLL_DELAY_SECONDS,
    DEFAULT_LAZY_SCROLL_STEPS,
)

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


def _clamp_knob(value: _Knob, ceiling: _Knob | None) -> _Knob:
    """Hold a knob at its ceiling when it has one, and pass it through when it does not."""
    return value if ceiling is None else min(value, ceiling)


# Session cookies harvested from browser renders, keyed by registrable domain.
# Lets HTTP_CFFI_IMPERSONATE replay a browser-minted session on retry.
# Bounded with an LRU so a long-lived process crawling many domains cannot
# grow this dict without bound.


LAZY_SCROLL_STEPS = DEFAULT_LAZY_SCROLL_STEPS
LAZY_SCROLL_DELAY_SECONDS = DEFAULT_LAZY_SCROLL_DELAY_SECONDS


# Server-side clamp for caller-supplied action wait durations: a hostile
# policy must not be able to park a render (or, pre-fix, the event loop)
# for an arbitrary time.
_MAX_ACTION_WAIT_SECONDS = 10.0


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
    return max(1, min(requested_ms, int(_MAX_ACTION_WAIT_SECONDS * 1000)))


class BrowserActionsMixin:
    """The attributes this mixin reads off the renderer that mixes it in.

    Declared rather than left implicit: every method here touches
    ``self._async_sleep`` or a scroll/harvest knob owned by ``BrowserRenderer``,
    and a mixin that reads attributes nobody declares is a mixin whose
    contract lives only in the reader's head. Without this, moving a method into
    the mixin is a type error at every use, which is the wrong place to learn
    what it needs.

    Declared as annotations only (no values), so nothing is instantiated and no
    attribute is shadowed at runtime -- ``BrowserRenderer.__init__`` remains the
    single place these are set.
    """

    _async_sleep: Any
    _harvest_timeout_seconds: float
    _lazy_scroll_steps: int
    _lazy_scroll_delay_seconds: float

    async def _run_action_chain(
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
        N x ``_MAX_ACTION_WAIT_SECONDS``, and the exhaustion that followed said
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
            await self._async_sleep(min(float(action.get("seconds", 1)), _MAX_ACTION_WAIT_SECONDS))
        elif action_type == "execute_js":
            # Refuse arbitrary JS execution (RCE-class risk). Callers must
            # not be able to run scripts in the browser context.
            return

    async def _capture_element_screenshots(self, page: Any, selectors: list[str]) -> list[dict[str, Any]]:
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

    def _harvest_timeout_for(self, browser_profile: dict[str, Any] | None) -> float:
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

    def _scroll_config_for(self, browser_profile: dict[str, Any] | None) -> tuple[int, float]:
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

    async def _lazy_load_scroll(self, page: Any, browser_profile: dict[str, Any] | None = None) -> None:
        """Scroll to the bottom repeatedly to trigger lazy-loaded content."""
        steps, delay = self._scroll_config_for(browser_profile)
        for _ in range(steps):
            try:
                await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            except Exception:  # scrolling is best-effort
                return
            await self._async_sleep(delay)
