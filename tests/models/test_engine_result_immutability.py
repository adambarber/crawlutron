"""EngineCrawlResult is a frozen record: its collection fields must be too."""

# #603: ``frozen=True`` freezes only the reference, so a dataclass carrying
# list/dict fields let a caller mutate a "frozen" result's contents in place.

from __future__ import annotations

from typing import cast

import pytest

from crawlutron.models.crawl_result import EngineCrawlResult
from crawlutron.models.taxonomy import Classification
from tests._result_builders import _link, engine_result


def _rendered() -> EngineCrawlResult:
    return engine_result(
        "https://example.test/a",
        "rendered",
        links=["https://example.test/b"],
        validators={"etag": "W/1"},
    )


def test_header_mutation_is_refused():
    result = _rendered()
    with pytest.raises(TypeError):
        cast("dict[str, str]", result.headers)["x"] = "1"


def test_validator_mutation_is_refused():
    result = _rendered()
    with pytest.raises(TypeError):
        cast("dict[str, str]", result.validators)["etag"] = "forged"


def test_collection_mutation_is_refused():
    result = _rendered()
    with pytest.raises((AttributeError, TypeError)):
        cast("list[object]", result.attempted_strategies).append(object())
    with pytest.raises((AttributeError, TypeError)):
        cast("list[object]", result.discovered_links).append(object())
    with pytest.raises((AttributeError, TypeError)):
        cast("list[object]", result.classification_evidence).append(object())


def test_constructors_accept_sequences_and_store_immutable_views():
    """The construction edge normalizes: callers pass list/dict, the record holds
    tuple/Mapping -- the field's declared type is the truth about its contents."""
    result = EngineCrawlResult(
        original_url="https://example.test/a",
        normalized_url=None,
        final_url=None,
        status="success",
        classification=Classification.rendered,
        selected_strategy=None,
        attempted_strategies=(),
        http_status=None,
        headers={"etag": "W/1"},
        content_type=None,
        html=None,
        text=None,
        extraction_method=None,
        quality_score=0.0,
        classification_evidence=(),
        discovered_links=(_link("https://example.test/b", "https://example.test/a"),),
        validators={},
    )
    assert isinstance(result.attempted_strategies, tuple)
    assert isinstance(result.classification_evidence, tuple)
    assert isinstance(result.discovered_links, tuple)
    # A mapping read view, not a dict the caller could put back.
    assert result.headers["etag"] == "W/1"
    assert not hasattr(result.headers, "__setitem__")
    assert not hasattr(result.validators, "__setitem__")
