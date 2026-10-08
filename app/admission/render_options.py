"""The single decision point for effective browser-render options.

Render options were once assembled by hand at every entry point, and the
copies had already drifted: the engine's browser strategy hard-coded the
capture knobs while policy-derived reads respected the caller elsewhere,
and the ``/v1/render`` builder silently dropped ``browser_profile`` so no
worker knob (viewport, lazy scroll, resource mode...) could reach the
worker from that path.

``resolve_render_options`` is now the only place request-side options and
server defaults merge into a :class:`app.models.RenderOptions`; the AST
detector in ``tests/invariants/test_result_shape.py`` fails the build on any new
construction site. Callers adapt their own source shape (a render-options
dict, a crawl policy) into the two mappings the resolver reads.

One knob, two owners — this is the distinction the drifted sites collapsed:

* **capture** (``include_html`` / ``include_text`` as the browser worker
  consumes them): the ``/v1/render`` caller legitimately controls it, so
  the request value wins there. On the crawl path the *pipeline* owns it:
  ``_browser_strategy_result`` feeds the captured HTML to markdown
  extraction, classification, and link discovery before anything applies
  the caller's output knob, so honoring the payload knob for capture would
  silently degrade results instead of saving work.
* **payload** (what survives into the spilled/shaped result): always the
  caller's ``browser_profile.include_html``, read through
  :func:`policy_wants_html` — never re-derived at each strip site.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.models.crawl_policy import CrawlPolicy
from app.models.render_options import RenderOptions


def _pick(request: Mapping[str, Any] | None, defaults: Mapping[str, Any], key: str) -> Any:
    """Caller value wins; an explicit ``None`` counts as absent (raw-dict
    callers send nulls for knobs they did not set; excluding them is the
    pydantic layer's job, and the resolver is below that layer)."""
    if request is not None and request.get(key) is not None:
        return request[key]
    return defaults.get(key)


def resolve_render_options(
    request_options: Mapping[str, Any] | None,
    defaults: Mapping[str, Any] | None = None,
) -> RenderOptions:
    """Merge request-side render options over server defaults.

    ``request_options`` is whatever the caller supplied (a ``/v1/render``
    options dict, or a pipeline's fixed requirement set — callers own the
    policy of *which* of their knobs are caller-controlled and pass the
    rest through ``defaults``). Unknown keys are ignored: a stale client
    sending a removed knob (``wait_until``) must keep working.
    ``browser_profile`` is forwarded as a defensive copy — dropping it was
    the asymmetry that made every worker knob unreachable from
    ``/v1/render``.
    """
    request = request_options or {}
    base = defaults or {}
    kwargs: dict[str, Any] = {}
    timeout = _pick(request, base, "timeout_seconds")
    if timeout is not None:
        kwargs["timeout_seconds"] = int(timeout)
    retries = _pick(request, base, "max_retries")
    if retries is not None:
        kwargs["max_retries"] = int(retries)
    for key in ("respect_robots", "include_html", "include_text"):
        value = _pick(request, base, key)
        if value is not None:
            kwargs[key] = bool(value)
    kwargs["browser_profile"] = dict(_pick(request, base, "browser_profile") or {})
    return RenderOptions(**kwargs)


def policy_wants_html(policy: CrawlPolicy) -> bool:
    """The caller's payload knob for a crawl policy: keep raw HTML or strip it.

    The one reading of ``policy.browser_profile["include_html"]`` allowed
    outside this module; strip sites (engine spill, HTTP strategy) call this
    instead of re-deriving the expression, which is how "who owns this
    knob" drifted per call site in the first place.
    """
    return bool(policy.browser_profile.get("include_html", True))


__all__ = ["policy_wants_html", "resolve_render_options"]
