"""Batch-shape resilience property.

``_shape_all`` reshapes each item inside a per-item guard so one poison item
cannot 500 the batch. The original defect had two halves: a bare except
swallowed the exception with NO logging, and a healthy item could end up
silently reclassified to ``site_error``. These property-style tests pin the
CLASS of defect, not the instance: across many randomized batches (stdlib
random, fixed seed sweep), the batch always survives, every healthy item
keeps its real classification, every degraded position still reports
declared taxonomy members, and every poison degradation is logged.
"""

from __future__ import annotations

import random
from typing import Any

from app.api.response_shaping import (
    EnvelopeOptions,
    _shape_all,
    abuild_unified_crawl_response,
    build_unified_crawl_response,
    render_result_to_unified,
)
from app.api_models import CrawlStatus
from app.constants import CrawlClassification, status_for_classification

SEEDS = range(12)
BATCH_MIN, BATCH_MAX = 1, 12
HEALTHY_PROBABILITY = 0.5

# Real classifications the engine legitimately emits at these status tiers,
# deliberately excluding site_error so "healthy items keep their real
# classification" also pins "healthy items are never reclassified to
# site_error" (the silent-reclassification half of the original bug).
HEALTHY_CLASSIFICATIONS = (
    "rendered",
    "not_modified",
    "captcha_detected",
    "network_error",
    "not_found",
    "robots_disallowed",
)

POISON_KINDS = ("missing_keys", "wrong_types", "boom_mapping", "none", "scalar")


class _BoomMapping(dict):
    """An item whose own read path explodes (the model_dump-raises shape)."""

    def get(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("model_dump exploded")


def _healthy(index: int, classification: str) -> dict[str, Any]:
    status = CrawlStatus(status_for_classification(classification))
    url = f"https://ok.example/page{index}"
    item: dict[str, Any] = {
        "original_url": url,
        "normalized_url": url,
        "final_url": url,
        "status": status.value,
        "classification": classification,
        "selected_strategy": "HTTP_DECLARED_CRAWLER",
        "attempted_strategies": [],
        "error": None if status is CrawlStatus.success else "deterministic failure",
        "http_status": 200,
        "headers": {},
        "text": f"body {index}",
    }
    if status is CrawlStatus.success:
        item["markdown"] = f"# Page {index}"
        item["html"] = f"<html><head><title>Page {index}</title></head><body>hi</body></html>"
    return item


def _healthy_render(index: int) -> dict[str, Any]:
    """A browser ``CrawlResult`` dict: the shape /v1/render feeds ``adapt``."""
    url = f"https://ok.example/render{index}"
    return {
        "url": url,
        "final_url": url,
        "status": "success",
        "classification": "rendered",
        "elapsed_ms": 25,
        "error": None,
        "html": f"<html><head><title>R{index}</title></head><body>x</body></html>",
        "text": "x",
        "metadata": {"title": f"R{index}", "browser_reused": True},
    }


def _poison(kind: str, index: int) -> Any:
    if kind == "missing_keys":
        return {}
    if kind == "wrong_types":
        bad = _healthy(index, "rendered")
        bad["headers"] = "not-a-dict"
        bad["attempted_strategies"] = "not-a-list"
        bad["discovered_links"] = 5
        return bad
    if kind == "boom_mapping":
        return _BoomMapping()
    if kind == "none":
        return None
    return 42


def _random_batch(
    rng: random.Random,
) -> tuple[list[Any], list[tuple[str, str] | None], list[str | None]]:
    """A batch of healthy/poison items plus the per-position expectations.

    Each position is either ``(status, classification)`` — a healthy item
    whose real classification must survive shaping — or ``None`` (a poison
    position, whose only contract is "the batch survives and the taxonomy is
    honored"). The third list records the poison kind per position so the
    sweep can prove every strategy actually ran.
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
    """The randomized batch survives _shape_all with its healthy items intact."""

    def test_every_seed_survives_shape_all(self):
        seen_kinds: set[str] = set()
        seen_healthy = 0
        for seed in SEEDS:
            rng = random.Random(seed)
            batch, expected, kinds = _random_batch(rng)
            seen_kinds.update(kind for kind in kinds if kind is not None)
            seen_healthy += sum(1 for kind in kinds if kind is None)
            shaped = _shape_all(batch)
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
                batch, options=EnvelopeOptions(request_id=f"r{seed}")
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
            shaped = _shape_all(batch, adapt=render_result_to_unified)
            _check_shaped(shaped, expected)
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
            _shape_all(batch)
        warnings = [r for r in caplog.records if r.name == "app.api.response_shaping"]
        assert len(warnings) == 4, (
            "one warning per poison item, none for healthy ones — the silent "
            "site_error reclassification must stay impossible to miss"
        )
        assert all(r.levelname == "WARNING" for r in warnings)
        assert all(r.exc_info for r in warnings), "the swallowed exception must be logged"

    def test_poison_error_field_carries_a_stable_marker(self):
        """Callers can tell 'we could not shape this' from a real site failure."""
        # A dict missing every required key is the per-item guard's subject: the
        # declared item type is a result dict, and a dict the shaper cannot read
        # is what "malformed" means here. A ``None`` item degrades through the
        # same ``except`` and to the same marker, but it is not an item the
        # signature admits, so the probe stays inside the contract it is pinning.
        shaped = _shape_all([{}])
        assert shaped[0].error == "result_shaping_failed"
        shaped_url = _shape_all([{"original_url": "https://x.example/a", "status": None}])
        assert shaped_url[0].original_url == "https://x.example/a"
