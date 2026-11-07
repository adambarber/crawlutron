"""Construction enforces membership; the gate rejects non-members."""

from __future__ import annotations

import pytest

from tests._policy_factories import canonical
from tests.chokepoint_walks import app_offenders


def test_crawl_strategy_name_members_match_vocabulary():
    """The ``CrawlStrategyName`` Literal must not drift from the vocabulary.

    mypy rejects ``Final`` inside ``Literal``, so the members are spelled out
    and this pins them to the constants."""
    from typing import get_args

    from crawlutron.models.taxonomy import CrawlStrategyName
    from crawlutron.vocabulary.strategies import (
        BROWSER_HEADED_WARMED,
        BROWSER_HEADLESS,
        HTTP_CFFI_IMPERSONATE,
        HTTP_DECLARED_CRAWLER,
    )

    declared = {
        HTTP_DECLARED_CRAWLER,
        HTTP_CFFI_IMPERSONATE,
        BROWSER_HEADLESS,
        BROWSER_HEADED_WARMED,
    }
    assert set(get_args(CrawlStrategyName)) == declared


def test_classification_literal_covers_the_enum_exactly():
    """``Classification`` and ``CrawlClassification`` name the same taxonomy.

    mypy evaluates neither inside ``Literal``, so both are spelled out and this
    alone says they are one set."""
    from typing import get_args

    from crawlutron.models.taxonomy import Classification
    from crawlutron.vocabulary.taxonomy import CrawlClassification

    declared = set(get_args(Classification))
    enum = {member.value for member in CrawlClassification}
    assert enum - declared == set(), (
        "every CrawlClassification member needs a Classification Literal member:"
        f" missing {sorted(enum - declared)} (crawlutron/models/taxonomy.py)"
    )
    assert declared - enum == set(), (
        "a Classification member with no CrawlClassification member is downgraded"
        f" to site_error at response shaping: unknown {sorted(declared - enum)}"
    )


def test_crawl_error_is_the_pipeline_additions_over_classification():
    """The pipeline-only codes are a real split, not a second copy of the taxonomy.

    ``CrawlError`` derives from the union so membership cannot drift; a
    silently deleted condition narrows the ``error`` field callers key on."""
    from crawlutron.models.taxonomy import PIPELINE_ONLY_CRAWL_ERRORS, Classification, literal_members

    pipeline_only = literal_members(PIPELINE_ONLY_CRAWL_ERRORS)
    classification = literal_members(Classification)
    assert pipeline_only == {
        "robots_txt_disallowed",
        "robots_txt_refused",
        "robots_txt_unreachable",
        "browser_worker_unavailable",
        "unsupported_strategy",
        "retries_exhausted",
    }, sorted(pipeline_only)
    assert not pipeline_only & classification, (
        "a code in both halves is a page outcome misfiled as a pipeline"
        f" condition: {sorted(pipeline_only & classification)}"
    )


# Each is a list nobody extends, so the class re-emerged at every field not on the list.


def test_result_primitives_emit_only_declared_literal_members():
    """Every literal value handed to a result primitive is a member of its
    field's declared ``Literal`` -- the generic, field-axis-closed form of the
    per-field detectors. Tree-wide: a primitive in a new module is covered."""
    from tests.membership_gate import generic_literal_field_offenders, literal_typed_field_members

    field_members = literal_typed_field_members()
    offenders = app_offenders(generic_literal_field_offenders, members=field_members)
    assert not offenders, (
        "a literal passed to a result primitive must be a member of its field's"
        " declared Literal, or the strict enum at response shaping rejects the whole"
        f" batch: {'; '.join(offenders)}"
    )


def test_generic_membership_subsumes_the_per_field_detectors():
    """The generic sweep covers every field the per-field detectors guarded.

    The transcribed six-name list missed live primitives; the derived registry
    is closed by construction, licensing the list's deletion.
    """
    from tests.membership_gate import RESULT_DATACLASS_NAMES, RESULT_PRIMITIVES, literal_typed_field_members

    members = literal_typed_field_members()
    # The three escaped axes the per-field detectors were written for.
    assert "classification" in members and "site_error" in members["classification"]
    assert "status" in members and "failed" in members["status"]
    # The six-name list the per-field detectors transcribed.
    transcribed = {
        "failed_engine_result",
        "build_engine_result",
        "engine_result_from_exception",
        "skipped_engine_result",
        "crawl_result_from_exception",
        "terminal_crawl_result",
    }
    missed = RESULT_PRIMITIVES - transcribed
    assert (
        missed
        == {
            "binary_no_text_result",
            "crawl_result_from_engine_result",
            "terminal_engine_result",
        }
        | RESULT_DATACLASS_NAMES
    ), (
        "the derived registry must cover the primitives and dataclasses the"
        f" transcribed list omitted: {sorted(missed)}"
    )


def test_result_construction_enforces_membership_at_the_dataclass():
    """A result dataclass built ANYWHERE rejects a non-member value.

    Every dataclass validates its Literal fields in ``__post_init__``, so no
    build site can escape membership checking; a bad direct construction raises."""
    from crawlutron.models.crawl_result import EngineCrawlResult
    from crawlutron.models.serialization import from_wire, to_dict
    from tests._policy_factories import UNRESTRICTED_HOST_POLICY
    from tests._result_builders import engine_result

    # The all-member baseline, named once and DERIVED rather than restated. to_dict,
    # not asdict: the Mapping fields are not copyable, and the wire record is the truth.
    member_engine_result = dict(to_dict(engine_result("http://a/", "rendered")))
    # A wire-shaped record rebuilds through the same constructor (from_wire), so the
    # all-member build is checked against the taxonomy it is a sample of.
    assert from_wire(EngineCrawlResult, member_engine_result, policy=UNRESTRICTED_HOST_POLICY)

    # A non-member on each Literal axis raises at construction.
    for field, invented in (
        ("status", "Bogus"),
        ("classification", "Bogus"),
        ("selected_strategy", "BROWSER_INVENTED"),
    ):
        poisoned = dict(member_engine_result)
        poisoned[field] = invented
        with pytest.raises(ValueError, match=f".{field}="):
            from_wire(EngineCrawlResult, poisoned, policy=UNRESTRICTED_HOST_POLICY)


def test_the_crawl_result_attempt_rejects_bad_error_codes():
    """The CrawlError axis: a stable code outside the declared taxonomy is rejected."""
    from dataclasses import asdict

    from crawlutron.models.crawl_result import CrawlResult
    from crawlutron.models.crawl_strategy_attempt import CrawlStrategyAttempt

    member_crawl_result = CrawlResult(
        url=canonical("http://a/"),
        attempt=CrawlStrategyAttempt(
            strategy="BROWSER_HEADLESS",
            status="success",
            classification="rendered",
            http_status=200,
            content_type=None,
            elapsed_ms=0,
        ),
        final_url=None,
        html=None,
        text=None,
        attempts=1,
        domain="example.com",
    )
    attempt_fields = asdict(member_crawl_result)["attempt"]
    with pytest.raises(ValueError, match=r".error="):
        CrawlStrategyAttempt(**{**attempt_fields, "error": "worker_failed"})


# =========================================================================== NAMED-CONSTANT provenance
# (program 2) A literal detector is blind to a Name.


def test_result_primitives_take_taxonomy_constants_not_foreign_namespaces():
    """A constant from one taxonomy must not be consumed as a member of
    another. Value membership cannot see this -- the colliding value is what
    made the historical defect invisible."""
    from tests.membership_gate import literal_typed_field_members, namespace_constant_offenders

    field_members = literal_typed_field_members()
    offenders = app_offenders(namespace_constant_offenders, members=field_members)
    assert not offenders, (
        "a constant from a foreign namespace was consumed as a taxonomy member; the"
        " value collides with a real member which is exactly why a value check misses"
        f" it: {'; '.join(offenders)}"
    )


# --------------------------------------------------------------------------- The runtime gate (program 1+2
# closure): the class dies at the boundary


def test_the_membership_gate_rejects_a_non_member_value():
    """The AST detectors see source text; the historical escape used a local variable.

    The gate is the structural closure: whatever indirection a value took, it
    must pass the primitive to become a result.
    """
    from tests.membership_gate import NonMemberValue, assert_member, assert_optional_member

    members = frozenset({"rendered", "site_error"})
    assert_member("rendered", field="classification", members=members, where="_test")
    with pytest.raises(NonMemberValue):
        assert_member("binary_no_text", field="classification", members=members, where="_test")
    # None is admitted for the optional Literal fields.
    assert_optional_member(None, field="strategy", members=frozenset({"BROWSER_HEADLESS"}), where="_test")
    with pytest.raises(NonMemberValue):
        assert_optional_member(
            "BROWSER_DECLARED_CRAWLER", field="strategy", members=frozenset({}), where="_test"
        )


def test_the_gate_sees_the_local_variable_indirection_the_ast_detectors_cannot():
    """The exact historical shape, end to end.

    A local of a wrong-namespace constant handed to the primitive as a bare
    Name fires no kwarg-level AST detector; the gate does, at the boundary.
    """
    import asyncio

    from tests.membership_gate import NonMemberValue, field_member_sets

    members = field_member_sets()
    classification_members = members["classification"]

    async def scenario() -> None:
        # `classification` is a local whose provenance no AST walk can resolve from the call site alone.
        classification = "totally_made_up"
        from tests.membership_gate import assert_result_fields

        assert_result_fields(
            {"classification": classification},
            where="http.http_strategy_result",
            members=members,
        )

    with pytest.raises(NonMemberValue):
        asyncio.run(scenario())
    # A real member through the same indirection passes.
    assert "binary_no_text" in classification_members


def test_the_gate_would_have_caught_the_shipped_defect():
    """``EXTRACTION_METHOD_BINARY_NO_TEXT``'s value IS a legal member, so this
    is the shape a value check admits: the PROVENANCE detector names the
    defect, the gate bars any wrong value of any provenance."""
    from tests.membership_gate import NonMemberValue, assert_result_fields, field_member_sets

    members = field_member_sets()
    # A genuinely non-member value through the same local-variable shape.
    with pytest.raises(NonMemberValue):
        assert_result_fields(
            {"classification": "not_a_real_member"},
            where="http.http_strategy_result",
            members=members,
        )
