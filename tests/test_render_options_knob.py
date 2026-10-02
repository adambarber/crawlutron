"""RenderOptions.wait_until was an advertised-but-inert knob.

The browser strategy hardcodes goto(wait_until="domcontentloaded") and
implements the documented "network_idle_or_timeout" settle semantic in
_wait_for_ready_state. A second settle path would compete with that, so the
field was removed from RenderOptions, the /v1/render schema, and the MCP tool
descriptions. These tests pin the removal.

Sending ``wait_until`` is now REFUSED rather than ignored, which reverses what
this file previously asserted. The old rule treated an unknown key as harmless,
but this particular key was advertised in a past schema and is now inert, so an
upgrading caller who still sends it was left believing they controlled settle
behaviour while nothing happened -- permanently, and invisibly. That is the same
"believed applied, never applied" outcome the input-side extra=forbid rule
exists to end, and this module's own header called the knob advertised-but-inert.
A 422 naming the field costs the caller one round trip and tells them the knob is
gone; silence costs them a debugging session and still teaches them nothing.

Nothing here reads ``app/api/render.py`` for the token. The request model is
where a stale key arrives, the dataclass is where it would take effect, and
``resolve_render_options`` ignoring an unknown key is proven behaviourally in
``tests/crawl/test_render_options_resolver.py``. Whether the endpoint's module
mentions the string is not a property of the endpoint: a tree-wide chokepoint
already forces every RenderOptions through that one resolver, so the source read
was a fourth restatement of a property two other tests own.
"""

import pytest
from pydantic import ValidationError

from app.models.render_options import RenderOptions


class TestWaitUntilKnobRemoved:
    def test_render_options_has_no_wait_until_field(self):
        assert "wait_until" not in RenderOptions.__dataclass_fields__

    def test_render_request_refuses_wait_until_by_name(self):
        from app.api_models import RenderRequest

        with pytest.raises(ValidationError) as exc:
            RenderRequest(url="http://example.com/", wait_until="load")
        assert any("wait_until" in str(error["loc"]) for error in exc.value.errors())
