"""The browser action vocabulary: which actions exist and which are allowed.

A closed set answers WHICH actions a caller may name; value ceilings live in
``app.vocabulary.limits``, and the dispatching driver in ``app.transport``.
"""

from __future__ import annotations

from typing import Final, TypedDict


class BrowserAction(TypedDict, total=False):
    """One closed browser action: keys from ``BROWSER_ACTION_KEYS`` only.

    ``type`` selects the verb; ``selector``/``timeout``/``seconds``/``value``
    are the per-verb payload the chain's handlers read.
    """

    type: str
    selector: str
    timeout: int
    seconds: float
    value: str


# --- Browser action vocabulary: the single source of truth ---
# The driver's dispatch chain and the model's refusal check share this set.
BROWSER_ACTION_TYPES: Final[tuple[str, ...]] = (
    "click",
    "fill",
    "hover",
    "scroll",
    "scroll_bottom",
    "wait",
    "wait_seconds",
    "execute_js",
)

#: Actions a caller may request. ``execute_js`` is excluded by subtraction
#: from the vocabulary above, so a new action cannot widen what runs.
ALLOWED_BROWSER_ACTION_TYPES: Final[frozenset[str]] = frozenset(BROWSER_ACTION_TYPES).difference(
    {"execute_js"}
)

#: The actions that cannot do anything without naming what to act on; the
#: chain guards them with ``and selector``, and the model refuses up front.
ACTIONS_REQUIRING_SELECTOR: Final[frozenset[str]] = frozenset({"click", "fill", "hover", "scroll", "wait"})

#: The top-level keys a ``browser_profile`` may carry, read off the
#: consumers: an unread key is inert, so only keys with a real reader count.
BROWSER_PROFILE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "viewport",
        "resource_mode",
        "storage_state",
        "actions",
        "include_html",
        "include_screenshot",
        "screenshot_selectors",
        "auto_accept_cookie_consent",
        "lazy_scroll_steps",
        "lazy_scroll_delay_seconds",
        "cookie_harvest_timeout_seconds",
    }
)

#: Every key one ``browser_profile.actions`` entry may carry, read off the
#: chain's own ``action.get(...)`` calls: an unnamed key fills nothing.
BROWSER_ACTION_KEYS: Final[frozenset[str]] = frozenset({"type", "selector", "timeout", "seconds", "value"})

#: Most element screenshots in one render. Each is a full base64 PNG appended to
#: the result metadata, so this bounds the response body as well as the render.
