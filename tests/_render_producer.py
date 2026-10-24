"""Call the ``/v1/render`` producer the way an endpoint test needs to; it is a
plain coroutine, not a route, and every direct call spelled the same two
arguments (default settings, a worker) next to the per-test payload.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from tests._browser_doubles import _fake_browser

if TYPE_CHECKING:
    from app.models.result_record import RenderEnvelope


async def render_stub_request(payload: dict[str, Any], *, worker=None) -> RenderEnvelope:
    """The envelope ``/v1/render`` produces for `payload`, with a worker defaulting
    to success on a fixed body -- the shape an endpoint test asserts on; pass
    ``worker`` for scenarios that are about the worker itself.
    """
    from app.api.render import render_request
    from app.config.settings import CrawlerSettings
    from app.models.render_request import RenderRequest

    return await render_request(
        RenderRequest.model_validate(payload),
        settings=CrawlerSettings.from_mapping(),
        worker=_fake_browser(html="<html></html>", text="t") if worker is None else worker,
    )


__all__ = ["render_stub_request"]
