"""RenderOptions.wait_until was an advertised-but-inert knob.

The browser strategy hardcodes goto(wait_until="domcontentloaded") and
implements the documented "network_idle_or_timeout" settle semantic in
_wait_for_ready_state. A second settle path would compete with that,
so the field was removed from RenderOptions, the /v1/render schema, and
the MCP tool descriptions. These tests pin the removal and that callers
sending wait_until are not broken (the unknown key is simply ignored).

Nothing here reads ``app/api/render.py`` for the token. The request model is
where a stale key arrives, the dataclass is where it would take effect, and
``resolve_render_options`` ignoring an unknown key is proven behaviourally in
``tests/crawl/test_render_options_resolver.py``. Whether the endpoint's module
mentions the string is not a property of the endpoint: a tree-wide chokepoint
already forces every RenderOptions through that one resolver, so the source read
was a fourth restatement of a property two other tests own.
"""

from app.models import RenderOptions


class TestWaitUntilKnobRemoved:
    def test_render_options_has_no_wait_until_field(self):
        assert "wait_until" not in RenderOptions.__dataclass_fields__

    def test_render_request_accepts_and_ignores_wait_until(self):
        from app.api_models import RenderRequest

        request = RenderRequest(url="http://example.com/", wait_until="load")
        assert "wait_until" not in request.model_dump(exclude_none=True)
