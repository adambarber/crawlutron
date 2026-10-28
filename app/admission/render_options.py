"""Single place request-side browser-render options merge over server
defaults into :class:`app.models.RenderOptions`; capture is pipeline-owned
on the crawl path, payload is the caller's ``include_html`` via :func:`policy_wants_html`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict, cast

from app.models.crawl_policy import CrawlPolicy
from app.models.crawl_policy_vocabulary import refuse_oversized_profile_strings
from app.models.json_value import JsonValue
from app.models.render_options import RenderOptions
from app.vocabulary.limits import MAX_HTTP_TIMEOUT_SECONDS, MAX_RETRIES

if TYPE_CHECKING:
    from collections.abc import Mapping

    from app.models.browser_profile_record import BrowserProfileRecord

__all__ = ["RenderOptionsWire", "policy_wants_html", "resolve_render_options"]


class RenderOptionsWire(TypedDict, total=False):
    """The render knobs a caller may send; open-keyed so unknown keys never reach RenderOptions.

    ``browser_profile`` travels separately: the resolver validates and types it.
    """

    timeout_seconds: int
    max_retries: int
    respect_robots: bool
    include_html: bool
    include_text: bool


def _pick(
    request: Mapping[str, JsonValue] | None,
    defaults: Mapping[str, JsonValue],
    key: str,
) -> JsonValue | None:
    """Caller value wins; an explicit ``None`` counts as absent (raw-dict
    callers send nulls for knobs they did not set; excluding them is the
    pydantic layer's job, and the resolver is below that layer)."""
    if request is not None and request.get(key) is not None:
        return request[key]
    return defaults.get(key)


def resolve_render_options(
    request_options: Mapping[str, JsonValue] | None,
    defaults: Mapping[str, JsonValue] | None = None,
) -> RenderOptions:
    """Merge caller render options over server defaults -- caller wins, an explicit ``None`` counts as absent.

    Unknown keys are ignored (a stale client's removed knob keeps working);
    ``browser_profile`` is forwarded as a defensive copy so worker knobs stay reachable.
    """
    request: Mapping[str, JsonValue] = request_options or {}
    base: Mapping[str, JsonValue] = defaults or {}
    kwargs: RenderOptionsWire = {}
    # #444: the clamp binds the CALLER's side only -- the defaults side is
    # operator/policy-owned and ceilinged by its own validators already.
    timeout = request.get("timeout_seconds")
    if isinstance(timeout, (int, float, str)):
        kwargs["timeout_seconds"] = min(int(timeout), MAX_HTTP_TIMEOUT_SECONDS)
    elif isinstance(base_timeout := base.get("timeout_seconds"), (int, float, str)):
        kwargs["timeout_seconds"] = int(base_timeout)
    retries = request.get("max_retries")
    if isinstance(retries, (int, float, str)):
        kwargs["max_retries"] = min(int(retries), MAX_RETRIES)
    elif isinstance(base_retries := base.get("max_retries"), (int, float, str)):
        kwargs["max_retries"] = int(base_retries)
    for key in ("respect_robots", "include_html", "include_text"):
        value = _pick(request, base, key)
        if value is not None:
            kwargs[key] = bool(value)
    # #444: an unsized profile string is per-request memory a pooled renderer
    # holds; the crawl boundary already refused it, so this is the render edge.
    raw_profile = _pick(request, base, "browser_profile")
    profile = dict(raw_profile) if isinstance(raw_profile, dict) else {}
    refuse_oversized_profile_strings(profile)
    # This resolver is the validation boundary: sizes refused above, the
    # vocabulary closed the key set — the raw wire dict crosses as the record.
    return RenderOptions(**kwargs, browser_profile=cast("BrowserProfileRecord", profile))


def policy_wants_html(policy: CrawlPolicy) -> bool:
    """The caller's payload knob: keep raw HTML or strip it, read once via
    ``policy.browser_profile["include_html"]``. Strip sites call this to avoid
    drift — the sole reading of that field allowed outside this module.
    """
    return bool(policy.browser_profile.get("include_html", True))
