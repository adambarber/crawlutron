"""RenderOptions.wait_until was an advertised-but-inert knob: the strategy hardcodes
its settle semantic, so the field is removed everywhere and sending it is now REFUSED
(422), not ignored -- silence taught an upgrading caller nothing, one round trip does.
"""

import pytest
from pydantic import ValidationError

from crawlutron.models.render_options import RenderOptions


class TestWaitUntilKnobRemoved:
    def test_render_options_has_no_wait_until_field(self):
        assert "wait_until" not in RenderOptions.__dataclass_fields__

    def test_render_request_refuses_wait_until_by_name(self):
        from crawlutron.models.render_request import RenderRequest

        with pytest.raises(ValidationError) as exc:
            RenderRequest(url="http://example.com/", wait_until="load")
        assert any("wait_until" in str(error["loc"]) for error in exc.value.errors())
