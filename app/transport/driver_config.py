"""Driver knobs: profiles, launch kwargs, storage validation, scroll bounds."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from camoufox.async_api import AsyncCamoufox

from app.config.env import parse_bool_value
from app.vocabulary.limits import DEFAULT_LAZY_SCROLL_DELAY_SECONDS, DEFAULT_LAZY_SCROLL_STEPS

# Records keep the driver's name: a test pins the refusal's logger, and the
# validator moved here without moving its surface.
logger = logging.getLogger("app.transport.browser_driver")


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


#: The numeric knobs a profile carries. Constrained rather than ``float``
#: so ``_clamp_knob`` returns an int for int readers, not a widened float.
_Knob = TypeVar("_Knob", int, float)


def _clamp_knob[Knob: (int, float)](value: Knob, ceiling: Knob | None) -> Knob:
    """Hold a knob at its ceiling when it has one, and pass it through when it does not."""
    return value if ceiling is None else min(value, ceiling)


LAZY_SCROLL_STEPS = DEFAULT_LAZY_SCROLL_STEPS


LAZY_SCROLL_DELAY_SECONDS = DEFAULT_LAZY_SCROLL_DELAY_SECONDS


# Server-side clamp for caller action waits: a hostile policy must not
# park a render (or the event loop) for an arbitrary time.
MAX_ACTION_WAIT_SECONDS = 10.0


def ACTION_WAIT_MS(action: dict[str, Any]) -> int:
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


def create_camoufox_browser(**kwargs: Any):
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


def browser_kwargs(browser_profile: dict[str, Any] | None, cache_dir: str | Path | None) -> dict[str, Any]:
    """Launch-only kwargs for the Camoufox/Playwright browser factory."""
    kwargs: dict[str, Any] = {
        "headless": headless_enabled(),
        "humanize": True,
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


def page_kwargs(browser_profile: dict[str, Any] | None) -> dict[str, Any]:
    """Page/context kwargs, applied at ``browser.new_page`` time."""
    profile = browser_profile or {}
    viewport = profile.get("viewport")
    if viewport == "mobile":
        return {"viewport": {"width": 390, "height": 844}}
    return {"viewport": {"width": 1365, "height": 768}}


def harvest_timeout_for(browser_profile: dict[str, Any] | None, default_seconds: float) -> float:
    """Cookie-harvest wait bound: policy profile beats settings, ceilinged."""
    from app.vocabulary.limits import MAX_COOKIE_HARVEST_TIMEOUT_SECONDS

    profile = browser_profile or {}
    return _profile_float(
        profile,
        "cookie_harvest_timeout_seconds",
        default_seconds,
        MAX_COOKIE_HARVEST_TIMEOUT_SECONDS,
    )


def scroll_config_for(
    browser_profile: dict[str, Any] | None, default_steps: int, default_delay: float
) -> tuple[int, float]:
    """Lazy-scroll steps/delay: policy profile beats settings, ceilinged."""
    from app.vocabulary.limits import MAX_LAZY_SCROLL_DELAY_SECONDS, MAX_LAZY_SCROLL_STEPS

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
