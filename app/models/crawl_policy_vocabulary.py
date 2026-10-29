from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlsplit

from app.safety.header_values import reject_injectable_header_value
from app.vocabulary.limits import MAX_HEADER_VALUE_BYTES
from app.vocabulary.request_profiles import REQUEST_PROFILE_KEYS

__all__ = [
    "REQUEST_PROFILE_KEYS_JSON",
    "close_browser_profile_vocabulary",
    "close_request_profile_vocabulary",
    "narrows_credential_scope",
    "refuse_oversized_profile_strings",
]


def close_browser_profile_vocabulary(profile: Mapping[str, object]) -> Mapping[str, object]:
    """Refuse inert keys and actions in browser_profile (a misspelled action used to
    be silently skipped). Vocabulary comes from app.vocabulary so this and the
    renderer read one set; refusals are ValueError (a 422), not a TypeError's 500.
    """
    from app.vocabulary.browser import ALLOWED_BROWSER_ACTION_TYPES, BROWSER_PROFILE_KEYS

    if not isinstance(profile, dict):
        return profile
    allowed = ", ".join(sorted(ALLOWED_BROWSER_ACTION_TYPES))

    unknown_keys = sorted(set(profile) - BROWSER_PROFILE_KEYS)
    if unknown_keys:
        raise ValueError(
            f"browser_profile keys not permitted: {', '.join(unknown_keys)}; "
            f"supported keys are: {', '.join(sorted(BROWSER_PROFILE_KEYS))}"
        )

    _refuse_over_ceiling_knobs(profile)
    refuse_oversized_profile_strings(profile)

    actions = profile.get("actions")
    if actions is None:
        return profile
    if not isinstance(actions, list):
        raise ValueError(
            f"browser_profile actions must be a list of action objects, got {type(actions).__name__}"
        )
    if len(actions) > MAX_BROWSER_ACTIONS:
        raise ValueError(
            f"browser_profile actions may carry at most {MAX_BROWSER_ACTIONS} entries, got {len(actions)}"
        )

    for index, action in enumerate(actions):
        _refuse_unusable_action(action, index, allowed)
    return profile


def _refuse_over_ceiling_knobs(profile: Mapping[str, object]) -> None:
    """Refuse a closed key whose value is larger than the work it buys is worth.
    The closed vocabulary bounds which keys exist, not how large their values may
    be, and the consumers only clamp -- so this is the tell, not a silent early-finish render.
    """
    from app.vocabulary.limits import (
        MAX_COOKIE_HARVEST_TIMEOUT_SECONDS,
        MAX_LAZY_SCROLL_DELAY_SECONDS,
        MAX_LAZY_SCROLL_STEPS,
        MAX_SCREENSHOT_SELECTORS,
    )

    for key, ceiling in (
        ("lazy_scroll_steps", MAX_LAZY_SCROLL_STEPS),
        ("lazy_scroll_delay_seconds", MAX_LAZY_SCROLL_DELAY_SECONDS),
        ("cookie_harvest_timeout_seconds", MAX_COOKIE_HARVEST_TIMEOUT_SECONDS),
    ):
        value = profile.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > ceiling:
            raise ValueError(f"browser_profile {key} may be at most {ceiling}, got {value}")

    selectors = profile.get("screenshot_selectors")
    if isinstance(selectors, list) and len(selectors) > MAX_SCREENSHOT_SELECTORS:
        raise ValueError(
            f"browser_profile screenshot_selectors may carry at most {MAX_SCREENSHOT_SELECTORS} "
            f"entries, got {len(selectors)}"
        )


def refuse_oversized_profile_strings(
    profile: Mapping[str, object], ceiling: int = MAX_HEADER_VALUE_BYTES
) -> None:
    """Refuse any string in a ``browser_profile`` over ``ceiling``.

    An unsized selector or header-like value is per-request memory a pooled
    renderer holds; the ceiling is the header-value interop number.
    """
    for key, value in profile.items():
        _refuse_oversized_value(key, value, ceiling)


def _refuse_oversized_value(key: str, value: object, ceiling: int) -> None:
    """One profile value against the string ceiling, nested shapes included."""
    if isinstance(value, dict):
        for nested in value.values():
            _refuse_oversized_value(key, nested, ceiling)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _refuse_oversized_value(key, item, ceiling)
    elif isinstance(value, str) and len(value) > ceiling:
        raise ValueError(f"browser_profile {key} may carry at most {ceiling} characters, got {len(value)}")


def _refuse_unusable_action(action: object, index: int, allowed: str) -> None:
    """Refuse one action the renderer would accept and then do nothing about.
    Split out so the refusals read as distinct failures; ``allowed`` is passed in
    so this checks one action instead of reading the vocabulary a second time.
    """
    from app.vocabulary.browser import (
        ACTIONS_REQUIRING_SELECTOR,
        ALLOWED_BROWSER_ACTION_TYPES,
        BROWSER_ACTION_KEYS,
    )

    if not isinstance(action, dict):
        raise ValueError("each browser_profile action must be an object with a 'type'")

    unknown_keys = sorted(set(action) - BROWSER_ACTION_KEYS)
    if unknown_keys:
        raise ValueError(
            f"browser_profile action keys not permitted: {', '.join(unknown_keys)}; "
            f"supported keys are: {', '.join(sorted(BROWSER_ACTION_KEYS))}"
        )

    action_type = str(action.get("type", "")).lower()
    if action_type == "execute_js":
        raise ValueError(
            "execute_js actions are not permitted in browser_profile: this service does not "
            "run caller-supplied script in the browser context"
        )
    if action_type not in ALLOWED_BROWSER_ACTION_TYPES:
        raise ValueError(
            f"browser_profile action type {action_type!r} is not permitted; allowed types are: {allowed}"
        )
        # a selector-less click is a no-op there, so the request returns 200 with a page that
        # loaded and did nothing; refusing here is the only place the caller learns why.
    if action_type in ACTIONS_REQUIRING_SELECTOR and not action.get("selector"):
        raise ValueError(
            f"browser_profile action at index {index} is of type {action_type!r}, which acts "
            "on a named element, so it must carry a non-empty 'selector'"
        )


def close_request_profile_vocabulary(profile: Mapping[str, object]) -> Mapping[str, object]:
    """Refuse inert keys and malformed values in request_profile: a misspelled key
    used to do nothing while reporting success, and a malformed value raised a 500
    inside the header builder. Refusals are ValueError (a 422), never a TypeError.
    """
    if not isinstance(profile, dict):
        return profile

    unknown_keys = sorted(set(profile) - REQUEST_PROFILE_KEYS)
    if unknown_keys:
        raise ValueError(
            f"request_profile keys not permitted: {', '.join(unknown_keys)}; "
            f"supported keys are: {', '.join(sorted(REQUEST_PROFILE_KEYS))}"
        )

    _refuse_malformed_profile_values(profile)
    return profile


def _refuse_malformed_profile_values(profile: Mapping[str, object]) -> None:
    """Refuse a value of the wrong shape behind a permitted key.
    Split from the vocabulary check: "key exists" and "value is the thing the
    key means" are different questions, and the latter used to raise inside the header builder.
    """
    from app.vocabulary.request_profiles import (
        COOKIE_JAR_VALUES,
        MAX_COOKIE_NAMESPACE_LENGTH,
        MAX_PROFILE_COOKIE_HOSTS,
        MAX_PROFILE_COOKIE_PAIRS,
        MAX_PROFILE_POOL_ENTRIES,
        REQUEST_PROFILE_FLAG_KEYS,
        REQUEST_PROFILE_POOL_KEYS,
        REQUEST_PROFILE_TEXT_KEYS,
    )

    for key, value in profile.items():
        if key in REQUEST_PROFILE_FLAG_KEYS and not isinstance(value, bool):
            raise ValueError(f"request_profile {key} must be true or false, got {type(value).__name__}")
        if key in REQUEST_PROFILE_TEXT_KEYS and not isinstance(value, str):
            raise ValueError(f"request_profile {key} must be a string, got {type(value).__name__}")

    jar = profile.get("cookie_jar")
    if isinstance(jar, str) and jar not in COOKIE_JAR_VALUES:
        raise ValueError(
            f"request_profile cookie_jar {jar!r} is not permitted; valid values are: "
            f"{', '.join(sorted(COOKIE_JAR_VALUES))}"
        )

    namespace = profile.get("cookie_namespace")
    if isinstance(namespace, str) and len(namespace) > MAX_COOKIE_NAMESPACE_LENGTH:
        raise ValueError(
            f"request_profile cookie_namespace may carry at most {MAX_COOKIE_NAMESPACE_LENGTH} "
            f"characters, got {len(namespace)}"
        )

    for key in sorted(REQUEST_PROFILE_POOL_KEYS & profile.keys()):
        _refuse_malformed_pool(profile, key, MAX_PROFILE_POOL_ENTRIES)

    if "cookies" in profile:
        _refuse_malformed_cookie_map(profile, MAX_PROFILE_COOKIE_HOSTS, MAX_PROFILE_COOKIE_PAIRS)


def _refuse_malformed_pool(profile: Mapping[str, object], key: str, ceiling: int) -> None:
    """Refuse a header pool that is not a bounded list of bounded strings.
    Split so the wrong-container and wrong-contents failures read separately; a
    ``[None]`` used to reach the header builder and raise from inside it.
    """
    pool = profile[key]
    if not isinstance(pool, list):
        raise ValueError(f"request_profile {key} must be a list of strings, got {type(pool).__name__}")
    if len(pool) > ceiling:
        raise ValueError(f"request_profile {key} may carry at most {ceiling} entries, got {len(pool)}")
    for entry in pool:
        if not isinstance(entry, str):
            raise ValueError(f"request_profile {key} entries must be strings, got {type(entry).__name__}")
        # The pool entry lands in User-Agent/Accept-Language verbatim, so it is a
        # header value here too: the header-value law and ceiling apply at this edge.
        reject_injectable_header_value(key, entry)
        if len(entry) > MAX_HEADER_VALUE_BYTES:
            raise ValueError(
                f"request_profile {key} entries may carry at most {MAX_HEADER_VALUE_BYTES} "
                f"characters, got {len(entry)}"
            )


def _refuse_malformed_cookie_map(profile: Mapping[str, object], host_ceiling: int, pair_ceiling: int) -> None:
    """Refuse a static cookie map that is unbounded or malformed.
    Without the bounds, a huge map became distinct cache slots (content_cache_key
    serialises the whole header map); the no-namespace single-tenant path stays allowed.
    """
    cookies = profile["cookies"]
    if not isinstance(cookies, dict):
        raise ValueError(
            f"request_profile cookies must be a mapping of host to cookies, got {type(cookies).__name__}"
        )
    if len(cookies) > host_ceiling:
        raise ValueError(f"request_profile cookies may name at most {host_ceiling} hosts, got {len(cookies)}")
    for host, pairs in cookies.items():
        if not isinstance(pairs, dict):
            raise ValueError(
                f"request_profile cookies for host {host!r} must be a mapping of name to value, "
                f"got {type(pairs).__name__}"
            )
        if len(pairs) > pair_ceiling:
            raise ValueError(
                f"request_profile cookies for host {host!r} may carry at most {pair_ceiling} "
                f"entries, got {len(pairs)}"
            )
        for name, value in pairs.items():
            if not isinstance(name, str) or not isinstance(value, (str, int, float)):
                raise ValueError(
                    f"request_profile cookie {name!r} for host {host!r} must be a string name and a "
                    "string or number value"
                )


def narrows_credential_scope(key: str) -> bool:
    """True when a credential key names less than a whole origin.
    A key that cannot reach every path on its host is narrower than the per-origin
    lookup it would normalize into; the empty path is just "scheme://host".
    """
    parsed = urlsplit(key.strip())
    if parsed.query or parsed.fragment or parsed.username is not None or parsed.password is not None:
        return True
    return parsed.path not in ("", "/")


MAX_BROWSER_ACTIONS = 64


MAX_HEADERS_PER_REQUEST = 64


REQUEST_PROFILE_KEYS_JSON: dict[str, object] = {"keys": sorted(REQUEST_PROFILE_KEYS)}
