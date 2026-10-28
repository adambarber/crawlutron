"""Page interaction: shadow DOM, consent, screenshots, teardown."""

from __future__ import annotations

import base64
import contextlib
from collections.abc import Callable

from app.models.screenshot_diagnostics import ScreenshotDiagnostics
from app.transport.driver_config import async_sleep
from app.transport.render_page import PageElement, RenderPage


async def safe_close_page(page: RenderPage) -> None:
    """Best-effort page teardown."""
    with contextlib.suppress(Exception):  # best-effort teardown
        await page.close()


async def flatten_shadow_dom(page: RenderPage) -> None:
    # Move each open shadow root's children into its light-DOM host (closed
    # roots are inaccessible); collect first, then move, to avoid mutation.
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


_CONSENT_SELECTORS = [
    # Common cookie consent button selectors and text patterns:
    # generic button/anchor with common accept text, then ID/class patterns.
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

_CONSENT_FALLBACK_JS = """() => {
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
}"""


async def auto_accept_cookie_consent(page: RenderPage, enabled: bool, sleep: Callable[[float], None]) -> bool:
    """Auto-click cookie consent buttons when the caller opts in."""
    if not enabled:
        return False
    try:
        for selector in _CONSENT_SELECTORS:
            with contextlib.suppress(Exception):
                element = await page.query_selector(selector)
                if element:
                    # Check if element is visible
                    is_visible = await element.is_visible()
                    if is_visible:
                        await element.click()
                        await async_sleep(sleep, 0.5)  # Brief wait for consent to register
                        return True
        # Fallback: evaluate JS to find buttons by text content
        clicked = await page.evaluate(_CONSENT_FALLBACK_JS)
        return bool(clicked)
    except Exception:
        return False


async def capture_element_screenshots(page: RenderPage, selectors: list[str]) -> list[ScreenshotDiagnostics]:
    """Capture targeted element screenshots as base64."""
    shots: list[ScreenshotDiagnostics] = []
    for selector in selectors:
        with contextlib.suppress(Exception):  # best-effort per-element capture
            element: PageElement | None = await page.query_selector(selector)
            if element is None:
                continue
            data = base64.b64encode(await element.screenshot()).decode("ascii")
            shots.append({"selector": selector, "screenshot": data})
    return shots
