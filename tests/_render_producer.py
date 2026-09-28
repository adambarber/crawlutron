"""Call the ``/v1/render`` producer the way an endpoint test needs to.

``render_request`` is a plain coroutine, not a route: a test that wants the
envelope the response adapter reads calls it directly, and every such call
spelled out the same two arguments -- default settings, and a worker that
returns a rendered result -- next to the payload the test was actually about.
The payload is the per-test part; the rest is the arrangement.
"""

from tests._browser_doubles import _fake_browser


async def render_stub_request(payload: dict, *, worker=None) -> dict:
    """The envelope ``/v1/render`` produces for `payload`.

    The worker defaults to one that succeeds with a fixed body, which is the
    shape an endpoint test is asserting on. Pass ``worker`` for the scenarios
    that are about the worker itself -- one that raises, one that records the
    jobs it was handed.
    """
    from app.api.render import render_request
    from app.settings import CrawlerSettings

    return await render_request(
        payload,
        settings=CrawlerSettings.from_mapping(),
        worker=_fake_browser(html="<html></html>", text="t") if worker is None else worker,
    )


__all__ = ["render_stub_request"]
