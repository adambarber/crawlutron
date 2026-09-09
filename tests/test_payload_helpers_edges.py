"""Payload helper edge cases: strategy validation and default handling."""

from coverage_helpers import *


class TestPayloadHelpersEdges:
    """Cover remaining branches in payload_helpers.py"""

    def test_payload_policy_invalid_limits(self):
        from app.crawl.payload_helpers import _payload_policy
        from app.settings import CrawlerSettings

        with pytest.raises(ValueError) as cm:
            _payload_policy({"policy": {"limits": "not a dict"}}, CrawlerSettings.from_mapping())
        assert str(cm.value) == "invalid_policy_limits"
        with pytest.raises(ValueError) as cm:
            _payload_policy(
                {"policy": {"limits": {"max_depth": "not an int"}}}, CrawlerSettings.from_mapping()
            )
        assert str(cm.value) == "invalid_policy_limits"
