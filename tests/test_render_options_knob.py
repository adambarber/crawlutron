"""N10: RenderOptions.wait_until was an advertised-but-inert knob.

The browser strategy hardcodes goto(wait_until="domcontentloaded") and
implements the documented "network_idle_or_timeout" settle semantic in
_wait_for_ready_state (M9). A second settle path would compete with that,
so the field was removed from RenderOptions, the /v1/render schema, and
the MCP tool descriptions. These tests pin the removal and that callers
sending wait_until are not broken (the unknown key is simply ignored).
"""


from app.models import RenderOptions


class TestWaitUntilKnobRemoved:
    def test_render_options_has_no_wait_until_field(self):
        assert "wait_until" not in RenderOptions.__dataclass_fields__

    def test_render_request_accepts_and_ignores_wait_until(self):
        from app.api_models import RenderRequest

        request = RenderRequest(url="http://example.com/", wait_until="load")
        assert "wait_until" not in request.model_dump(exclude_none=True)

    def test_render_options_builder_does_not_read_wait_until(self):
        import inspect

        from app.api import render as render_module

        src = inspect.getsource(render_module)
        assert "wait_until" not in src
