"""One poison item must not 500 the batch -- a bare except once swallowed with
no log and silently reclassified healthy items to ``site_error``. Seeds pin
the class: batches survive, healthy items keep their type.
"""

from __future__ import annotations

import random
from dataclasses import replace
from typing import Any

from app.api.response_shaping import (
    EnvelopeOptions,
    abuild_unified_crawl_response,
    build_unified_crawl_response,
    build_unified_result,
    render_result_to_unified,
)
from app.models.crawl_result import CrawlResult, EngineCrawlResult
from app.models.crawl_status import CrawlStatus
from app.orchestration.render_result import terminal_attempt
from app.vocabulary.strategies import HTTP_DECLARED_CRAWLER
from app.vocabulary.taxonomy import CrawlClassification, status_for_classification
from tests._policy_factories import canonical
from tests._result_builders import engine_result

SEEDS = range(12)
BATCH_MIN, BATCH_MAX = 1, 12
HEALTHY_PROBABILITY = 0.5

# Real classifications the engine legitimately emits at these status tiers, deliberately excluding site_error
# so "healthy items keep their real classification" also pins "healthy items are never
HEALTHY_CLASSIFICATIONS = (
    "rendered",
    "not_modified",
    "captcha_detected",
    "network_error",
    "not_found",
    "robots_disallowed",
)

POISON_KINDS = ("not_a_result", "boom_mapping", "bad_evidence", "bad_validators", "none", "scalar")


class _BoomMapping(dict):
    """An item whose own read path explodes (the model_dump-raises shape)."""

    def get(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("model_dump exploded")


def _healthy(index: int, classification: str) -> EngineCrawlResult:
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


def _poison(kind: str, index: int) -> Any:
    """The survivors of records closing most shapes: a non-result, one that
    explodes on first read, and the OPEN payloads (``classification_evidence``,
    ``validators``) still ``dict[str, Any]`` by declaration -- so reachable.
    """
    if kind == "not_a_result":
        return {}
    if kind == "boom_mapping":
        return _BoomMapping()
    if kind == "bad_evidence":
        return replace(_healthy(index, "rendered"), classification_evidence=[{"detail": 42}])
    if kind == "bad_validators":
        return replace(_healthy(index, "rendered"), validators={"etag": {"not": "a string"}})
    if kind == "none":
        return None
    return 42


def _random_batch(
    rng: random.Random,
) -> tuple[list[Any], list[tuple[str, str] | None], list[str | None]]:
    """Per position, either ``(status, classification)`` for a healthy item whose
    real type must survive, or None for a poison slot (survival is its only
    contract); ``kinds`` lets the sweep prove every strategy ran.
    """
    batch: list[Any] = []
    expected: list[tuple[str, str] | None] = []
    kinds: list[str | None] = []
    for index in range(rng.randint(BATCH_MIN, BATCH_MAX)):
        if rng.random() < HEALTHY_PROBABILITY:
            classification = rng.choice(HEALTHY_CLASSIFICATIONS)
            batch.append(_healthy(index, classification))
            expected.append((status_for_classification(classification), classification))
            kinds.append(None)
        else:
            kind = rng.choice(POISON_KINDS)
            batch.append(_poison(kind, index))
            expected.append(None)
            kinds.append(kind)
    return batch, expected, kinds


def _assert_taxonomy_member(value: Any, enum: Any, what: str) -> None:
    assert isinstance(value, enum), f"{what}={value!r} is not a declared {enum.__name__} member"


def _check_shaped(shaped: list[Any], expected: list[tuple[str, str] | None]) -> None:
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
            batch, expected, kinds = _random_batch(rng)
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
            batch, expected, _ = _random_batch(rng)
            response = build_unified_crawl_response(batch, options=EnvelopeOptions(request_id=f"r{seed}"))
            _check_shaped(response.results, expected)

    async def test_async_builder_survives_the_same_batches(self):
        for seed in SEEDS:
            rng = random.Random(2000 + seed)
            batch, expected, _ = _random_batch(rng)
            response = await abuild_unified_crawl_response(
                batch, adapt=build_unified_result, options=EnvelopeOptions(request_id=f"r{seed}")
            )
            _check_shaped(response.results, expected)

    async def test_adapter_batch_survives_and_keeps_render_classifications(self):
        """/v1/render adapts inside the same guard (the once-unguarded comprehension)."""
        for seed in SEEDS:
            rng = random.Random(3000 + seed)
            batch: list[Any] = []
            expected: list[tuple[str, str] | None] = []
            for index in range(rng.randint(BATCH_MIN, BATCH_MAX)):
                if rng.random() < HEALTHY_PROBABILITY:
                    batch.append(_healthy_render(index))
                    expected.append(("success", "rendered"))
                else:
                    batch.append(_poison(rng.choice(POISON_KINDS), index))
                    expected.append(None)
            shaped = await abuild_unified_crawl_response(batch, adapt=render_result_to_unified)
            _check_shaped(shaped.results, expected)
            response = await abuild_unified_crawl_response(
                batch, adapt=render_result_to_unified, options=EnvelopeOptions(request_id=f"a{seed}")
            )
            _check_shaped(response.results, expected)


class TestPoisonDegradesLoudly:
    """The original bug swallowed the exception silently; degradation is now logged."""

    def test_each_poison_item_logs_one_warning(self, caplog):
        batch = [
            _healthy(0, "rendered"),
            {},
            _poison("wrong_types", 1),
            None,
            _BoomMapping(),
        ]
        with caplog.at_level("WARNING", logger="app.api.response_shaping"):
            build_unified_crawl_response(batch)
        warnings = [r for r in caplog.records if r.name == "app.api.response_shaping"]
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
        shaped = build_unified_crawl_response([{}]).results
        assert shaped[0].error == "result_shaping_failed"
        # The fallback is best-effort by attribute, so a real result still reports which URL failed rather
        # than degrading to an empty one.
        shaped_url = build_unified_crawl_response([engine_result("https://x.example/a", "rendered")]).results
        assert shaped_url[0].original_url == "https://x.example/a"
