"""One poison item must not 500 the batch -- a bare except once swallowed with
no log and silently reclassified healthy items to ``site_error``. Seeds pin
the class: batches survive, healthy items keep their type.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import replace
from typing import cast

from crawlutron.api.response_shaping import (
    EnvelopeOptions,
    abuild_unified_crawl_response,
    build_unified_crawl_response,
    build_unified_result,
    render_result_to_unified,
)
from crawlutron.models.classification_evidence import ClassificationEvidenceRecord
from crawlutron.models.crawl_result import CrawlResult, EngineCrawlResult
from crawlutron.models.crawl_status import CrawlStatus
from crawlutron.models.result_factory import terminal_attempt
from crawlutron.models.taxonomy import Classification
from crawlutron.models.unified_crawl_result import UnifiedCrawlResult
from crawlutron.vocabulary.strategies import HTTP_DECLARED_CRAWLER
from crawlutron.vocabulary.taxonomy import CrawlClassification, status_for_classification
from tests._policy_factories import canonical
from tests._result_builders import engine_result

SEEDS = range(12)
BATCH_MIN, BATCH_MAX = 1, 12
HEALTHY_PROBABILITY = 0.5

#: Real classifications the engine legitimately emits at these status tiers, deliberately excluding site_error
#: so "healthy items keep their real classification" also pins "healthy items are never reclassified".
HEALTHY_CLASSIFICATIONS: tuple[Classification, ...] = (
    "rendered",
    "not_modified",
    "captcha_detected",
    "network_error",
    "not_found",
    "robots_disallowed",
)

POISON_KINDS = ("not_a_result", "boom_mapping", "bad_evidence", "bad_validators", "none", "scalar")

#: Poison items enter via an explicit cast per loop; the lie is localized there.
#: A well-typed record with an ill-typed extra key is the OPEN-payload poison.


class _BoomMapping(dict[str, object]):
    """An item whose own read path explodes (the model_dump-raises shape)."""

    def get(self, *args: object, **kwargs: object) -> object:
        raise RuntimeError("model_dump exploded")


def _healthy(index: int, classification: Classification) -> EngineCrawlResult:
    """A real engine result, built through the record not a dict: the dict form
    was only admissible while the shaper read untyped keys, so the property is
    about what goes wrong AT the boundary, not a shape no producer emits.
    """
    url = f"https://ok.example/page{index}"
    success = status_for_classification(classification) == "success"
    return engine_result(
        url,
        classification,
        final_url=url,
        selected_strategy=HTTP_DECLARED_CRAWLER,
        # A declared CrawlError member, not a free-form string: the record refuses an error outside its own
        # Literal, which is a check the dict form never got and this property never asked for.
        error=None if success else classification,
        http_status=200,
        text=f"body {index}",
        markdown=f"# Page {index}" if success else None,
        html=f"<html><head><title>Page {index}</title></head><body>hi</body></html>" if success else None,
    )


def _healthy_render(index: int) -> CrawlResult:
    """A real browser ``CrawlResult``: the record /v1/render feeds ``adapt``."""
    url = canonical(f"https://ok.example/render{index}")
    return CrawlResult(
        url=url,
        attempt=terminal_attempt(
            strategy=None,
            status="success",
            classification="rendered",
            elapsed_ms=25,
            error=None,
        ),
        final_url=url,
        html=f"<html><head><title>R{index}</title></head><body>x</body></html>",
        text="x",
        attempts=1,
        domain="ok.example",
        metadata={"title": f"R{index}", "browser_reused": True},
    )


#: Poison is NOT a result -- the guard's subject. Sweep loops cast it into
#: the element type; the point is that garbage AT the boundary is absorbed.
def _poison(kind: str, index: int) -> object:
    """The survivors of records closing most shapes: a non-result, one that
    explodes on first read, and the OPEN payloads (``classification_evidence``,
    ``validators``) still ``dict[str, Any]`` by declaration -- so reachable.
    """
    # `error=` is a declared CrawlError Literal; a healthy classification's error is None, so the
    # non-Literal poison classifications flow through `classification_evidence`/`validators` instead.
    healthy = _healthy(index, "rendered")
    if kind == "not_a_result":
        return {}
    if kind == "boom_mapping":
        return _BoomMapping()
    if kind == "bad_evidence":
        # A poison classification feeds the OPEN payload field, never `error=` (a declared Literal).
        evidence = cast("list[ClassificationEvidenceRecord]", [{"detail": 42}])
        return replace(healthy, classification_evidence=tuple(evidence))
    if kind == "bad_validators":
        bad = cast("dict[str, str]", {"etag": {"not": "a string"}})
        return replace(healthy, validators=bad)
    if kind == "none":
        return None
    return 42


#: The shapers' batch contract. Poison enters through one explicit
#: cast per loop: garbage AT that boundary is absorbed.
BatchItem = EngineCrawlResult | CrawlResult


def _engine_build(rng: random.Random, index: int) -> tuple[EngineCrawlResult, tuple[str, str]]:
    classification = rng.choice(HEALTHY_CLASSIFICATIONS)
    return _healthy(index, classification), (status_for_classification(classification), classification)


def _render_build(rng: random.Random, index: int) -> tuple[CrawlResult, tuple[str, str]]:
    return _healthy_render(index), ("success", "rendered")


def _random_batch[T](
    rng: random.Random,
    build: Callable[[random.Random, int], tuple[T, tuple[str, str]]],
) -> tuple[list[T], list[tuple[str, str] | None], list[str | None]]:
    """Per position, the builder's healthy (item, expectation) pair, or a poison
    slot cast to ``T`` — survival is poison's only contract; ``kinds`` lets the
    sweep prove every strategy ran.
    """
    batch: list[T] = []
    expected: list[tuple[str, str] | None] = []
    kinds: list[str | None] = []
    for index in range(rng.randint(BATCH_MIN, BATCH_MAX)):
        if rng.random() < HEALTHY_PROBABILITY:
            item, expectation = build(rng, index)
            batch.append(item)
            expected.append(expectation)
            kinds.append(None)
        else:
            kind = rng.choice(POISON_KINDS)
            # The cast IS the property: poison is not a result, and the guard must hold anyway.
            batch.append(cast(T, _poison(kind, index)))
            expected.append(None)
            kinds.append(kind)
    return batch, expected, kinds


def _assert_taxonomy_member(value: object, enum: type, what: str) -> None:
    assert isinstance(value, enum), f"{what}={value!r} is not a declared {enum.__name__} member"


def _check_shaped(shaped: list[UnifiedCrawlResult], expected: list[tuple[str, str] | None]) -> None:
    assert len(shaped) == len(expected), "one position per item: no drops, no 500"
    for result, expectation in zip(shaped, expected, strict=True):
        _assert_taxonomy_member(result.status, CrawlStatus, "status")
        _assert_taxonomy_member(result.classification, CrawlClassification, "classification")
        if expectation is None:
            continue
        status, classification = expectation
        assert result.status == status, f"healthy item status drifted: {result.status!r} != {status!r}"
        assert result.classification == classification, (
            f"healthy item silently reclassified: {result.classification!r} != {classification!r}"
        )


class TestBatchShapeProperty:
    """The randomized batch survives shaping with its healthy items intact."""

    def test_every_seed_survives_shaping(self):
        seen_kinds: set[str] = set()
        seen_healthy = 0
        for seed in SEEDS:
            rng = random.Random(seed)
            batch, expected, kinds = _random_batch(rng, _engine_build)
            seen_kinds.update(kind for kind in kinds if kind is not None)
            seen_healthy += sum(1 for kind in kinds if kind is None)
            shaped = build_unified_crawl_response(batch).results
            _check_shaped(shaped, expected)
        # The sweep is only meaningful if every poison strategy actually ran.
        assert seen_kinds == set(POISON_KINDS), f"poison strategies never exercised: {seen_kinds}"
        assert seen_healthy >= 12, "no healthy items across the sweep"

    def test_sync_and_async_builders_share_the_guard(self):
        """Both response builders must carry the same per-item contract."""
        for seed in SEEDS:
            rng = random.Random(1000 + seed)
            batch, expected, _ = _random_batch(rng, _engine_build)
            response = build_unified_crawl_response(batch, options=EnvelopeOptions(request_id=f"r{seed}"))
            _check_shaped(response.results, expected)

    async def test_async_builder_survives_the_same_batches(self):
        for seed in SEEDS:
            rng = random.Random(2000 + seed)
            batch, expected, _ = _random_batch(rng, _engine_build)
            response = await abuild_unified_crawl_response(
                batch, adapt=build_unified_result, options=EnvelopeOptions(request_id=f"r{seed}")
            )
            _check_shaped(response.results, expected)

    async def test_adapter_batch_survives_and_keeps_render_classifications(self):
        """The /v1/render door adapts inside the same guard (the once-unguarded
        comprehension); its batch is the render record, so the poison cast targets it."""
        for seed in SEEDS:
            rng = random.Random(3000 + seed)
            batch, expected, _ = _random_batch(rng, _render_build)
            shaped = await abuild_unified_crawl_response(batch, adapt=render_result_to_unified)
            _check_shaped(shaped.results, expected)
            response = await abuild_unified_crawl_response(
                batch, adapt=render_result_to_unified, options=EnvelopeOptions(request_id=f"a{seed}")
            )
            _check_shaped(response.results, expected)


class TestPoisonDegradesLoudly:
    """The original bug swallowed the exception silently; degradation is now logged."""

    def test_each_poison_item_logs_one_warning(self, caplog):
        # Same boundary lie as the sweep loops: poison enters via one cast, the guard absorbs it.
        batch = cast(
            list[EngineCrawlResult],
            [_healthy(0, "rendered"), {}, _poison("wrong_types", 1), None, _BoomMapping()],
        )
        with caplog.at_level("WARNING", logger="crawlutron.api.response_shaping"):
            build_unified_crawl_response(batch)
        warnings = [r for r in caplog.records if r.name == "crawlutron.api.response_shaping"]
        assert len(warnings) == 4, (
            "one warning per poison item, none for healthy ones — the silent "
            "site_error reclassification must stay impossible to miss"
        )
        assert all(r.levelname == "WARNING" for r in warnings)
        assert all(r.exc_info for r in warnings), "the swallowed exception must be logged"

    def test_poison_error_field_carries_a_stable_marker(self):
        """Callers can tell 'we could not shape this' from a real site failure."""
        # A dict missing every required key is the per-item guard's subject: the declared item type is a
        # result dict, and a dict the shaper cannot read is what "malformed" means here.
        shaped = build_unified_crawl_response(cast("list[EngineCrawlResult]", [{}])).results
        assert shaped[0].error == "result_shaping_failed"
        # The fallback is best-effort by attribute, so a real result still reports which URL failed rather
        # than degrading to an empty one.
        shaped_url = build_unified_crawl_response([engine_result("https://x.example/a", "rendered")]).results
        assert shaped_url[0].original_url == "https://x.example/a"
