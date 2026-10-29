"""The extractor metadata key-set must project to the wire model loudly (#302).

The adapter replaces the splat into an extra='ignore' model, so a key-set drift
between the extractor and the wire projection is a loud failure, not a silent drop.
"""

from __future__ import annotations

import pytest

from crawlutron.api.unified_shaping import build_unified_result
from crawlutron.models.crawl_result import EngineCrawlResult
from tests._result_builders import engine_result


def _item() -> EngineCrawlResult:
    return engine_result(
        "http://example.com/x",
        "rendered",
        html="<html><title>X</title></html>",
        markdown="# Body",
        text="Body",
        final_url="http://example.com/x",
    )


class TestMetadataResponseKeyDrift:
    def test_declared_extractor_keys_still_project(self, monkeypatch):
        def _real_extractor(item):
            return ("# markdown", {"title": "Example", "language": "en"})

        monkeypatch.setattr("crawlutron.api.unified_shaping.format_crawl_item", _real_extractor)
        result = build_unified_result(_item())
        assert result.metadata.title == "Example"
        assert result.metadata.language == "en"

    def test_undeclared_extractor_key_fails_loudly(self, monkeypatch):
        """A key-set drift (an extractor key with no wire field) must raise, never
        be silently dropped by the wire model's extra='ignore' default."""

        def _drifting_extractor(item):
            return ("# markdown", {"title": "x", "undeclared_key": "boom"})

        monkeypatch.setattr("crawlutron.api.unified_shaping.format_crawl_item", _drifting_extractor)
        with pytest.raises(ValueError, match="unhandled extractor metadata keys"):
            build_unified_result(_item())
