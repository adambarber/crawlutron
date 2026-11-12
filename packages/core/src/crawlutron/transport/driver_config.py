"""Driver knobs: profiles, launch kwargs, storage validation, scroll bounds."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TypeVar

from camoufox.async_api import AsyncCamoufox

from crawlutron.config.env import parse_bool_value
from crawlutron.models.browser_profile_record import BrowserProfileRecord
from crawlutron.vocabulary.browser import BrowserAction
from crawlutron.vocabulary.limits import DEFAULT_LAZY_SCROLL_DELAY_SECONDS, DEFAULT_LAZY_SCROLL_STEPS

# Records keep the driver's name: a test pins the refusal's logger, and the
# validator moved here without moving its surface.
logger = logging.getLogger("crawlutron.transport.browser_driver")

#: The numeric knobs a profile carries. Constrained rather than ``float``
#: so ``_clamp_knob`` returns an int for int readers, not a widened float.
_Knob = TypeVar("_Knob", int, float)


def _profile_knob[Knob: (int, float)](
    profile: Mapping[str, object],
    key: str,
    default: Knob,
    ceiling: Knob | None,
    coerce: Callable[[int | float | str], Knob],
) -> Knob:
    """Read a non-negative numeric knob from a browser_profile, never raising.

    ``ceiling`` clamps on the way (else ``max(0, value)``). Clamping matters here
    because a profile reaches a renderer as a plain dict, validator long gone.
    """
    try:
        raw = profile.get(key, default)
        if not isinstance(raw, (int, float, str)):
            return default
        value = coerce(raw)
        if value != value:  # NaN: keep the default rather than poisoning waits
            return default
    except (TypeError, ValueError):
        return default
    return _clamp_knob(type(default)(max(0, value)), ceiling)


def _profile_int(profile: Mapping[str, object], key: str, default: int, ceiling: int | None = None) -> int:
    """Read a non-negative int knob from a browser_profile, never raising."""
    return _profile_knob(profile, key, default, ceiling, lambda raw: int(raw))


def _profile_float(
    profile: Mapping[str, object], key: str, default: float, ceiling: float | None = None
) -> float:
    """Read a non-negative float knob; the float sibling of ``_profile_int``."""
    return _profile_knob(profile, key, default, ceiling, lambda raw: float(raw))


def _clamp_knob[Knob: (int, float)](value: Knob, ceiling: Knob | None) -> Knob:
    """Hold a knob at its ceiling when it has one, and pass it through when it does not."""
    return value if ceiling is None else min(value, ceiling)


LAZY_SCROLL_STEPS = DEFAULT_LAZY_SCROLL_STEPS


LAZY_SCROLL_DELAY_SECONDS = DEFAULT_LAZY_SCROLL_DELAY_SECONDS


# Server-side clamp for caller action waits: a hostile policy must not
# park a render (or the event loop) for an arbitrary time.
MAX_ACTION_WAIT_SECONDS = 10.0


def ACTION_WAIT_MS(action: BrowserAction) -> int:
    """Clamp a caller-supplied ``wait`` timeout the way ``wait_seconds`` is.

    Unbounded values park the render arbitrarily and 0 disables the timeout
    entirely; garbage falls back to the 5s default rather than failing.
    """
    try:
        requested_ms = int(action.get("timeout", 5000))
    except (TypeError, ValueError):
        requested_ms = 5000
    return max(1, min(requested_ms, int(MAX_ACTION_WAIT_SECONDS * 1000)))


def _refuse_storage_state_without_cache_dir(storage_state: str) -> None:
    """Log why a ``storage_state`` was dropped for lack of a ``cache_dir``: this
    refusal is not "path not allowed" -- an absent allowlist is the actionable
    fact (the old temp-dir allowlist was world-readable).
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
    """Sleep without blocking the event loop, through an injected clock -- both
    render halves wait (renderer between retries, driver between scroll steps),
    and the wait must never freeze the loop; one impl instead of two.
    """
    if seconds <= 0:
        return
    await asyncio.to_thread(sleep, seconds)


def create_camoufox_browser(**kwargs: object) -> AsyncCamoufox:
    """Factory for AsyncCamoufox browser."""
    return AsyncCamoufox(**kwargs)


def headless_enabled() -> bool:
    return parse_bool_value(
        os.environ.get("CRAWLUTRON_HEADLESS"),
        default=True,
        env_name="CRAWLUTRON_HEADLESS",
    )


def validate_storage_state_path(storage_state: str, cache_dir: str | Path | None) -> str | None:
    """A storage_state path under the cache dir, or None.

    Playwright reads a string as a file path, so only the cache dir qualifies.
    """
    from pathlib import Path

    with contextlib.suppress(Exception):
        path = Path(storage_state).resolve()
        if not cache_dir:
            _refuse_storage_state_without_cache_dir(storage_state)
            return None
        path.relative_to(Path(cache_dir).resolve())
        return str(path)
    return None


def browser_kwargs(
    browser_profile: BrowserProfileRecord | None, cache_dir: str | Path | None
) -> dict[str, object]:
    """Launch-only kwargs for the Camoufox/Playwright browser factory."""
    kwargs: dict[str, object] = {
        "headless": headless_enabled(),
        "humanize": True,
        "i_know_what_im_doing": True,
    }

    profile = browser_profile or {}

    if profile.get("resource_mode") == "reduced":
        kwargs["args"] = ["--blink-settings=imagesEnabled=false"]

    storage_state = profile.get("storage_state")
    if storage_state:
        safe_path = validate_storage_state_path(storage_state, cache_dir)
        if safe_path:
            kwargs["storage_state"] = safe_path
        else:
            logger.warning("invalid_storage_state_path", extra={"path": str(storage_state)})

    return kwargs


def page_kwargs(browser_profile: BrowserProfileRecord | None) -> dict[str, object]:
    """Page/context kwargs, applied at ``browser.new_page`` time."""
    profile = browser_profile or {}
    viewport = profile.get("viewport")
    # Playwright's page.route never intercepts a service worker's fetch(); only
    # the context-level service_workers="block" keeps a hostile SW from starting.
    if viewport == "mobile":
        return {"service_workers": "block", "viewport": {"width": 390, "height": 844}}
    return {"service_workers": "block", "viewport": {"width": 1365, "height": 768}}


def harvest_timeout_for(browser_profile: Mapping[str, object] | None, default_seconds: float) -> float:
    """Cookie-harvest wait bound: policy profile beats settings, ceilinged."""
    from crawlutron.vocabulary.limits import MAX_COOKIE_HARVEST_TIMEOUT_SECONDS

    profile = browser_profile or {}
    return _profile_float(
        profile,
        "cookie_harvest_timeout_seconds",
        default_seconds,
        MAX_COOKIE_HARVEST_TIMEOUT_SECONDS,
    )


def scroll_config_for(
    browser_profile: Mapping[str, object] | None, default_steps: int, default_delay: float
) -> tuple[int, float]:
    """Lazy-scroll steps/delay: policy profile beats settings, ceilinged."""
    from crawlutron.vocabulary.limits import MAX_LAZY_SCROLL_DELAY_SECONDS, MAX_LAZY_SCROLL_STEPS

    profile = browser_profile or {}
    return (
        _profile_int(profile, "lazy_scroll_steps", default_steps, MAX_LAZY_SCROLL_STEPS),
        _profile_float(
            profile,
            "lazy_scroll_delay_seconds",
            default_delay,
            MAX_LAZY_SCROLL_DELAY_SECONDS,
        ),
    )
