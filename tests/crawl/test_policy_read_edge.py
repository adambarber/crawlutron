"""``app/crawl/policy.py`` — the one read edge between a policy and the model.

Two edges, deliberately not the same function: :func:`read_policy` is total
and repairs, :func:`validate_wire_policy` is the API edge and rejects. These
tests exist because the difference between those two is the whole design and
nothing else in the tree states it — the repair in particular was once "give
back the declared defaults", which is total in the formal sense and actively
dangerous in the practical one.

The defect that shaped the repair: ``read_policy`` caught ``ValidationError``
and returned ``CrawlPolicy()``. A row with one bad field therefore came back
with *every* field defaulted, which is the widening direction for every limit
at once (``max_pages=0`` is unbounded for ``/v1/jobs``) and silently dropped
fields the caller had deliberately chosen -- a narrower timeout, an explicit
strategy chain, the browser profile. A read edge that cannot raise cannot
report the problem, so the repair has to be right rather than loud.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from app.crawl import policy as policy_module
from app.crawl.policy import (
    _ERROR_CODE_BY_FIELD,
    PolicyTier,
    discovery_flags,
    read_policy,
    stored_policy,
    validate_wire_policy,
)
from app.models.crawl_policy import CrawlPolicy
from app.models.crawl_policy_limits import CrawlPolicyLimits
from app.settings import CrawlerSettings

# A field the model will reject on its own, used wherever a test needs "this row
# has exactly one bad field" without hand-writing an invalid value inline.
_OVER_MAX_RETRIES = 999


class TestReadPolicyRepairsOnlyWhatFailed:
    """The repair is field-scoped: a rejected field defaults, its siblings live."""

    def test_a_rejected_field_falls_back_to_its_declared_default(self):
        resolved = read_policy({"limits": {"max_retries": _OVER_MAX_RETRIES}})

        assert resolved.limits.max_retries == CrawlPolicy().limits.max_retries

    def test_the_fields_the_caller_got_right_survive_the_rejection(self):
        """The defect: one bad bound returned defaults for the whole policy.

        Asserted as behaviour rather than as a diff, because the property is
        the interesting part: a *narrower* stated timeout and an explicit
        single-strategy chain both have to outlive a bad retry count, or a
        typo silently widens the crawl instead of costing the caller the one
        field they got wrong.
        """
        resolved = read_policy(
            {
                "strategies": ["BROWSER_HEADLESS"],
                "limits": {"timeout_seconds": 11, "max_retries": _OVER_MAX_RETRIES},
                "browser_profile": {"include_html": False, "viewport": "mobile"},
            }
        )

        assert resolved.limits.timeout_seconds == 11
        assert [strategy.value for strategy in resolved.strategies] == ["BROWSER_HEADLESS"]
        assert resolved.browser_profile == {"include_html": False, "viewport": "mobile"}

    def test_a_rejected_top_level_field_does_not_take_the_limits_with_it(self):
        resolved = read_policy({"scope_regex": "a" * 257, "limits": {"max_depth": 3, "max_pages": 500}})

        assert resolved.scope_regex is None
        assert (resolved.limits.max_depth, resolved.limits.max_pages) == (3, 500)

    def test_a_legacy_row_with_a_zero_bound_keeps_its_other_limits(self):
        """Rows the previous release persisted are inputs, not hypotheticals.

        That write edge never bounded ``timeout_seconds``, so ``0`` is on disk
        in real job rows today. The model rejects it (it is below ``ge=1``), and
        a rejected timeout must not cost the row its depth and page bound.
        """
        resolved = read_policy(
            {
                "strategies": ["BROWSER_HEADLESS"],
                "limits": {"timeout_seconds": 0, "max_depth": 3, "max_pages": 500},
            }
        )

        assert resolved.limits.timeout_seconds == CrawlPolicy().limits.timeout_seconds
        assert (resolved.limits.max_depth, resolved.limits.max_pages) == (3, 500)

    def test_a_rejected_list_member_drops_the_whole_list(self):
        """Not the single element: removing one renumbers the rest.

        ``strategies`` is an ordered execution chain, so an element dropped
        from the middle would silently promote the next strategy into that
        position. The list is the unit the model rejected, so it is the unit
        that goes.
        """
        resolved = read_policy(
            {
                "strategies": ["HTTP_DECLARED_CRAWLER", "NOT_A_STRATEGY", "BROWSER_HEADLESS"],
                "limits": {"max_depth": 4},
            }
        )

        assert [strategy.value for strategy in resolved.strategies] == [
            strategy.value for strategy in CrawlPolicy().strategies
        ]
        assert resolved.limits.max_depth == 4

    def test_a_valid_policy_is_untouched(self):
        """The repair must not fire on input the model already accepts."""
        raw = {"strategies": ["BROWSER_HEADLESS"], "limits": {"max_depth": 2, "max_pages": 9}}

        assert read_policy(raw) == CrawlPolicy.model_validate(raw)

    def test_an_already_built_model_is_returned_as_it_stands(self):
        model = CrawlPolicy.model_validate({"limits": {"max_depth": 2}})

        assert read_policy(model) is model


class TestReadPolicyIsTotal:
    """By contract it cannot raise, so every shape has to have an answer."""

    @pytest.mark.parametrize("raw", [None, 17, "text", ["a"], {"limits": "not a mapping"}])
    def test_every_non_policy_shape_resolves_to_a_policy(self, raw):
        assert isinstance(read_policy(raw), CrawlPolicy)

    def test_a_limits_that_is_not_a_mapping_keeps_the_other_fields(self):
        """Wrong-shaped is not the same as unreadable.

        A bad ``limits`` used to be a ``TypeError`` out of the validator, which
        the API turned into a 500 where the caller owed a 400. It must stay a
        field-level rejection here, so the strategy chain beside it survives.
        """
        resolved = read_policy({"limits": "not a mapping", "strategies": ["BROWSER_HEADLESS"]})

        assert [strategy.value for strategy in resolved.strategies] == ["BROWSER_HEADLESS"]

    def test_the_callers_mapping_is_not_mutated(self):
        """The API layer echoes this payload back, so repairing in place would
        change what the caller sent them."""
        raw = {"limits": {"max_retries": _OVER_MAX_RETRIES, "max_depth": 4}}

        read_policy(raw)

        assert raw == {"limits": {"max_retries": _OVER_MAX_RETRIES, "max_depth": 4}}


class TestValidateWirePolicyRejects:
    """The API edge. It does not repair — a caller error is reported, not guessed at."""

    @pytest.mark.parametrize(
        ("field", "value", "code"),
        [
            ("limits", {"timeout_seconds": 200}, "invalid_policy_limits"),
            ("limits", {"max_depth": 99}, "invalid_policy_limits"),
            ("limits", {"max_retries": _OVER_MAX_RETRIES}, "invalid_policy_limits"),
            ("scope_regex", "a" * 257, "invalid_policy"),
            ("headers", {"Cookie": "x=1"}, "invalid_policy_headers"),
            ("credentials", {"https://H.test": {"bearer_token": "t"}}, "invalid_policy_credentials"),
            ("strategies", ["NOT_A_STRATEGY"], "invalid_policy_strategy"),
            ("strategies", "BROWSER_HEADLESS", "invalid_policy_strategies"),
        ],
    )
    def test_rejection_carries_the_documented_code(self, field, value, code):
        with pytest.raises(ValueError, match=code):
            validate_wire_policy({field: value})

    def test_a_good_policy_validates_to_the_model(self):
        validated = validate_wire_policy({"strategies": ["BROWSER_HEADLESS"], "limits": {"max_depth": 2}})

        assert isinstance(validated, CrawlPolicy)
        assert validated.limits.max_depth == 2

    def test_the_callers_mapping_is_not_mutated(self):
        raw: dict[str, Any] = {"limits": {}}

        validate_wire_policy(raw)

        assert raw == {"limits": {}}

    def test_an_absent_max_pages_becomes_the_documented_default(self):
        """ "0 means the default page budget" is a wire meaning, not a model one.

        The model declares ``0`` as unbounded (the whole-site runner relies on
        it), so the absent/zero translation happens on the wire edge only --
        otherwise the runner's own unbounded jobs would be capped.
        """
        assert validate_wire_policy({"limits": {}}).limits.max_pages == policy_module.DEFAULT_MAX_PAGES
        assert read_policy({"limits": {}}).limits.max_pages == 0


class TestErrorCodeTable:
    """The per-field codes are derived from the model's declaration, so the
    table is pinned against it rather than trusted."""

    def test_every_model_field_names_a_code(self):
        """Gap direction is the one that bites.

        A code for a field the model does not declare is unreachable; a model
        field with no code falls through to a bare ``invalid_policy``, so the
        caller is told a policy is wrong without being told which part. Four
        fields were in that state and nothing tested it.
        """
        assert set(CrawlPolicy.model_fields) - set(_ERROR_CODE_BY_FIELD) == set()

    def test_a_deeper_failure_reports_its_own_block_not_the_outer_one(self):
        """A bad ``limits.max_depth`` is a limits problem, even when a
        top-level field failed too -- the caller needs the specific answer."""
        with pytest.raises(ValueError, match="invalid_policy_limits"):
            validate_wire_policy({"scope_regex": 5, "limits": {"max_depth": 99}})


class TestOperatorTier:
    """A deployment's default must be the same number on the API and job paths."""

    def _tier(self, **overrides: str) -> PolicyTier:
        return PolicyTier.from_settings(CrawlerSettings.from_mapping(overrides))

    def test_an_omitted_limit_takes_the_deployment_default(self):
        resolved = read_policy({}, self._tier(CRAWLUTRON_DEFAULT_TIMEOUT_SECONDS="77"))

        assert resolved.limits.timeout_seconds == 77

    def test_a_stated_limit_is_never_overwritten_by_the_deployment_default(self):
        resolved = read_policy(
            {"limits": {"timeout_seconds": 11}},
            self._tier(CRAWLUTRON_DEFAULT_TIMEOUT_SECONDS="77"),
        )

        assert resolved.limits.timeout_seconds == 11

    def test_the_tier_is_bounded_by_the_same_declaration_the_model_uses(self):
        """An operator cannot install a value a caller would be rejected for.

        Otherwise the deployment is a hole in the model: a per-request bound
        stops meaning anything for anyone who sets the env var. Read from the
        annotation rather than a restated copy of the bound, because a second
        copy of a number is one that will drift.
        """
        tier = self._tier(
            CRAWLUTRON_DEFAULT_TIMEOUT_SECONDS="500",
            CRAWLUTRON_DEFAULT_MAX_RETRIES="50",
        )

        assert _within_model_bounds("timeout_seconds", tier.timeout_seconds)
        assert _within_model_bounds("max_retries", tier.max_retries)

    def test_both_edges_resolve_the_same_omitted_limit(self):
        """The invariant itself, as the two call sites actually make the call.

        ``read_policy`` took the settings as an optional argument, so every
        job-path read passed nothing and answered with the model default while
        ``/v1/crawl`` answered with the operator tier -- same deployment, same
        policy, two different numbers, and no test comparing them.
        """
        tier = self._tier(CRAWLUTRON_DEFAULT_TIMEOUT_SECONDS="77")
        stored_row = read_policy({}, tier).model_dump()

        assert read_policy({}, tier).limits == read_policy(stored_row, tier).limits

    def test_no_tier_means_the_model_defaults(self):
        assert read_policy({}) == CrawlPolicy()


class TestDiscoveryFlags:
    """The optional block resolved once, so "absent" cannot become "on"."""

    def test_absent_means_off(self):
        assert discovery_flags(read_policy({})) == (False, False, False)

    def test_a_named_channel_is_on_and_the_rest_are_off(self):
        resolved = read_policy({"discovery": {"robots": True}})

        assert discovery_flags(resolved) == (True, False, False)

    def test_a_rejected_discovery_block_does_not_enable_anything(self):
        """The failure mode this function exists to stop.

        The block used to default every channel to on, so a row written before
        the field existed turned on robots/sitemap/feed crawling the caller
        never asked for. A block the model rejects must land on off, not on a
        default that grants.
        """
        resolved = read_policy({"discovery": {"not_a_channel": True}, "limits": {"max_depth": 2}})

        assert discovery_flags(resolved) == (False, False, False)
        assert resolved.limits.max_depth == 2


class TestStoredPolicy:
    """The write edge, mirroring the read edge in the other direction."""

    def test_a_dict_round_trips_through_the_read_edge(self):
        raw = {"strategies": ["BROWSER_HEADLESS"], "limits": {"max_depth": 2}}

        assert read_policy(stored_policy(raw)) == read_policy(raw)

    def test_a_model_round_trips_as_its_own_dump(self):
        model = read_policy({"strategies": ["BROWSER_HEADLESS"]})

        assert stored_policy(model) == model.model_dump()

    def test_a_corrupt_row_stores_the_repaired_policy_not_the_corrupt_text(self):
        """What goes on disk is what the model accepts.

        The pre-migration version forwarded whatever it was handed, so a row
        the read edge would have to repair was written out again on every save
        and the repair ran on every load instead of once.
        """
        assert stored_policy({"limits": {"max_retries": _OVER_MAX_RETRIES}}) == CrawlPolicy().model_dump()


def test_read_policy_does_not_call_model_validate_with_a_raw_model() -> None:
    """Guard against the total edge quietly becoming a raising one.

    ``read_policy`` is called from the job runner's control loop, where an
    exception is a crashed job rather than a rejected request. The totality is
    the contract; this fails the day a future edit lets an exception escape.
    """
    for raw in (None, 17, "text", ["a"], {"limits": "nope"}, {"limits": {"max_depth": -1}}):
        try:
            read_policy(raw)
        except ValidationError:  # pragma: no cover - the assertion
            pytest.fail(f"read_policy raised ValidationError for {raw!r}")


def _within_model_bounds(field: str, value: int) -> bool:
    """Is ``value`` inside the ``ge``/``le`` the model declares for ``field``?"""
    for meta in CrawlPolicyLimits.model_fields[field].metadata:
        if (lower := getattr(meta, "ge", None)) is not None and value < lower:
            return False
        if (upper := getattr(meta, "le", None)) is not None and value > upper:
            return False
    return True
