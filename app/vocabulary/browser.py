"""The browser action vocabulary: which actions exist and which are allowed.

A closed set answers WHICH actions a caller may name; the ceilings on their
values are in ``app.vocabulary.limits``. The driver that dispatches them lives in
``app.transport`` after #149's slice 2.
"""

from __future__ import annotations

from typing import Final

# ---------------------------------------------------------------------------
# Browser action vocabulary: THE single source of truth.
#
# ``BrowserDriver._dispatch_action`` recognises these by an if/elif chain and
# ``CrawlPolicy._close_browser_profile_vocabulary`` refuses one member of the same
# set at the model boundary. Two hand-written lists that must agree is the shape
# of bug this table exists to remove: the consumer silently no-ops a type it does
# not recognise, so a caller whose action was misspelled got a page that loaded
# with nothing done and nothing said. The agreement is asserted against the
# renderer's own chain rather than against a test-local copy, so renaming a
# branch there fails here instead of silently widening the gap.
#
# ``execute_js`` is listed rather than omitted because refusing it is a decision
# about a member of this vocabulary, not a fact about a different one. It is
# refused in BOTH places deliberately: at the model so a malicious payload never
# reaches a render, and at the consumer so a policy that reached the browser by
# any other route still cannot run script.
# ---------------------------------------------------------------------------
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

#: Actions a caller may actually request. ``execute_js`` is excluded by
#: subtraction from the vocabulary above rather than by restating it, so adding a
#: new action to the vocabulary cannot silently widen what a caller may invoke.
ALLOWED_BROWSER_ACTION_TYPES: Final[frozenset[str]] = frozenset(BROWSER_ACTION_TYPES).difference(
    {"execute_js"}
)

#: The actions that cannot do anything without naming what to act on. The chain
#: guards its target-taking branches with ``and selector``, so one of these
#: arriving without a ``selector`` falls past every branch and returns having done
#: nothing -- a 200 with a page that loaded and no interaction performed, which
#: the caller cannot distinguish from the action having worked. ``scroll_bottom``
#: and ``wait_seconds`` are absent because they address the page rather than an
#: element. This is the chain's requirement set, restated so the model can refuse
#: it up front; the two are asserted equal against the chain itself.
ACTIONS_REQUIRING_SELECTOR: Final[frozenset[str]] = frozenset({"click", "fill", "hover", "scroll", "wait"})

#: The top-level keys a ``browser_profile`` may carry. Read off the consumers,
#: which are where they take effect; an unknown key is inert, so accepting one
#: silently is the same failure as a misspelled action type -- and so is ALLOWING
#: one that no consumer reads. The set holds only keys with a real reader.
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

#: Every key a single ``browser_profile.actions`` entry may carry, ``type``
#: included, read off the chain's own ``action.get(...)`` calls and asserted
#: equal to them by a test. The reason it is read off the chain rather than
#: restated: the failure this prevents is a key the consumer reads and the
#: producer never named, so ``{"type": "fill", "selector": "#q", "val": "hi"}``
#: fills an empty string and reports success. A hand-kept second copy of this
#: set would be a third place to forget.
BROWSER_ACTION_KEYS: Final[frozenset[str]] = frozenset({"type", "selector", "timeout", "seconds", "value"})

#: Most element screenshots in one render. Each is a full base64 PNG appended to
#: the result metadata, so this bounds the response body as well as the render.
