"""Construction enforces membership; the gate rejects non-members."""

from __future__ import annotations

import ast

import pytest

from tests.chokepoint_walks import REPO_ROOT, app_offenders


def test_crawl_strategy_name_members_match_vocabulary():
    """The ``CrawlStrategyName`` Literal must not drift from the vocabulary.

    ``CrawlStrategyName`` cannot reference the ``Final`` constants directly inside
    ``Literal[...]`` (mypy rejects that), so its members are spelled as string
    literals. ``app/vocabulary/strategies.py`` remains the single source of truth;
    this pins the two together so a rename there cannot orphan the alias.
    """
    from typing import get_args

    from app.models.taxonomy import CrawlStrategyName
    from app.vocabulary.strategies import (
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

    Neither can be derived from the other: mypy will not evaluate
    ``*get_args()`` inside ``Literal[...]`` and will not read an enum, so
    ``Classification`` is spelled out and ``CrawlClassification`` is spelled out,
    and this is the only thing that says they are the same set. It was missing,
    which is the gap this whole taxonomy chain is built to close: a member added
    to the enum and emitted by a classifier passes every membership sweep (they
    check the enum direction), and then
    ``check_literal_fields`` rejects the whole batch at construction because the
    Literal never heard of it. The reverse -- a Literal member with no enum
    member -- reaches the strict enum at response shaping and takes the batch
    down there instead.

    Both directions are asserted, so neither half of the drift can be the one
    that slips through.
    """
    from typing import get_args

    from app.models.taxonomy import Classification
    from app.vocabulary.taxonomy import CrawlClassification

    declared = set(get_args(Classification))
    enum = {member.value for member in CrawlClassification}
    assert enum - declared == set(), (
        "every CrawlClassification member needs a Classification Literal member:"
        f" missing {sorted(enum - declared)} (app/models/taxonomy.py)"
    )
    assert declared - enum == set(), (
        "a Classification member with no CrawlClassification member is downgraded"
        f" to site_error at response shaping: unknown {sorted(declared - enum)}"
    )


def test_crawl_error_is_the_pipeline_additions_over_classification():
    """The pipeline-only codes are a real split, not a second copy of the taxonomy.

    ``CrawlError`` is defined as the union of ``Classification`` and
    ``PIPELINE_ONLY_CRAWL_ERRORS`` (app/models/taxonomy.py), so its membership is derived
    and cannot drift. What a union cannot check is whether the split is still
    meaningful: a code listed as a pipeline condition while also being a
    classification says a page outcome is a pipeline condition, and a pipeline
    condition deleted silently narrows the ``error`` field for callers who key on
    it as a stable code.
    """
    from app.models.taxonomy import PIPELINE_ONLY_CRAWL_ERRORS, Classification, literal_members

    pipeline_only = literal_members(PIPELINE_ONLY_CRAWL_ERRORS)
    classification = literal_members(Classification)
    assert pipeline_only == {
        "robots_txt_disallowed",
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


def test_generic_membership_detector_bites():
    """The generic detector fires on the historical regression shape, and only
    on it -- the proof that replacing the per-field list with a derived one did
    not silently turn the sweep into a no-op."""
    from tests.membership_gate import generic_literal_field_offenders, literal_typed_field_members

    members = literal_typed_field_members()
    assert members, "no Literal fields were enumerated; the generic sweep is a no-op"
    bad = ast.parse('def f():\n    return failed_engine_result(classification="totally_made_up")\n')
    hits = generic_literal_field_offenders(bad, REPO_ROOT / "app" / "_synthetic.py", members=members)
    assert len(hits) == 1 and "totally_made_up" in hits[0], hits
    # A real member literal passes.
    good = ast.parse('def f():\n    return failed_engine_result(classification="network_error")\n')
    assert not generic_literal_field_offenders(good, REPO_ROOT / "app" / "_synthetic.py", members=members)
    # A non-literal (the shape a correct caller writes) passes without comment.
    var = ast.parse("def f(c):\n    return failed_engine_result(classification=c)\n")
    assert not generic_literal_field_offenders(var, REPO_ROOT / "app" / "_synthetic.py", members=members)


def test_generic_membership_subsumes_the_per_field_detectors():
    """The generic sweep covers every field the three per-field detectors
    guarded, and every primitive the hard-coded list forgot.

    The historical detectors matched on a *transcribed* primitive list of six
    names (``_RESULT_PRIMITIVES`` below). Three live primitives that set
    classification/status/strategy were never on it -- ``binary_no_text_result``
    (which sets all three), ``crawl_result_from_engine_result``, and
    ``terminal_engine_result`` -- and neither were the direct dataclass
    constructions at five sites that bypass the primitives entirely. The
    derived registry has twelve entries and is closed by construction.

    This assert is the property that licenses deleting the per-field list: the
    coverage gap is not a matter of opinion, it is the set difference below.
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

    The historical escapes went through direct constructions outside
    result_helpers, so routing was policed geographically. That is no longer the
    load-bearing guarantee: every result dataclass validates its Literal fields
    in ``__post_init__``, so a build site cannot escape membership checking
    regardless of who calls it. This is the falsifiable version of that claim --
    a bad value at a direct construction site must raise, and a member must not.
    """
    from dataclasses import asdict

    from app.models.crawl_result import CrawlResult, CrawlStrategyAttempt, EngineCrawlResult
    from tests._result_builders import engine_result

    # The all-member baseline, named once and DERIVED rather than restated.
    member_engine_result = asdict(engine_result("http://a/", "rendered"))
    # Built, not spelled. Constructing the result directly hands each value to the field's own type, so the
    # all-member build is checked against the taxonomy it is a sample of.
    member_crawl_result = CrawlResult(
        url="http://a/",
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
    member_crawl_result_fields = asdict(member_crawl_result)

    # A direct construction with all-member values is accepted: the gate must not reject legitimate direct
    # builds (tests and the primitives both do it).
    CrawlStrategyAttempt(
        strategy="BROWSER_HEADLESS",
        status="success",
        classification="rendered",
        http_status=200,
        content_type=None,
        elapsed_ms=0,
    )
    EngineCrawlResult(**member_engine_result)
    # A non-member on each Literal axis raises at construction.
    for field, invented in (
        ("status", "Bogus"),
        ("classification", "Bogus"),
        ("selected_strategy", "BROWSER_INVENTED"),
    ):
        with pytest.raises(ValueError, match=f".{field}="):
            EngineCrawlResult(**{**member_engine_result, field: invented})
    # CrawlError axis: a stable code outside the declared taxonomy is rejected.
    with pytest.raises(ValueError, match=r"\.error="):
        CrawlStrategyAttempt(**{**member_crawl_result_fields["attempt"], "error": "worker_failed"})


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


def test_wrong_namespace_detector_bites():
    """The provenance detector fires on the historical shape and admits the
    correct one."""
    from tests.membership_gate import literal_typed_field_members, namespace_constant_offenders

    members = literal_typed_field_members()
    bad = ast.parse(
        'EXTRACTION_METHOD_BINARY_NO_TEXT = "binary_no_text"\n'
        "def f():\n"
        "    return failed_engine_result(classification=EXTRACTION_METHOD_BINARY_NO_TEXT)\n"
    )
    hits = namespace_constant_offenders(bad, REPO_ROOT / "app" / "_synthetic.py", members=members)
    assert len(hits) == 1 and "EXTRACTION_METHOD_" in hits[0], hits
    # The same constant used as its own field is the intended use.
    ok = ast.parse(
        'EXTRACTION_METHOD_BINARY_NO_TEXT = "binary_no_text"\n'
        "def f(m):\n"
        "    return build_engine_result(extraction_method=EXTRACTION_METHOD_BINARY_NO_TEXT)\n"
    )
    assert not namespace_constant_offenders(ok, REPO_ROOT / "app" / "_synthetic.py", members=members)
    # A parameter (what a correct caller writes) is not a constant: passes.
    param = ast.parse("def f(cls):\n    return failed_engine_result(classification=cls.site_error)\n")
    assert not namespace_constant_offenders(param, REPO_ROOT / "app" / "_synthetic.py", members=members)


# --------------------------------------------------------------------------- The runtime gate (program 1+2
# closure): the class dies at the boundary


def test_the_membership_gate_rejects_a_non_member_value():
    """The AST detectors see source text; the historical escape reached the
    primitive through a local variable, which no textual detector can see.
    The gate is the structural closure: whatever indirection a value took, it
    must pass the primitive to become a result."""
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
    """The exact historical shape, end to end: a local variable assigned a
    wrong-namespace constant, then handed to the primitive as a bare Name. No
    kwarg-level AST detector fires on this; the gate does, because it inspects
    the value at the boundary instead of the text at the call."""
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
    """``EXTRACTION_METHOD_BINARY_NO_TEXT`` has a value that IS a legal
    classification member -- so this is the shape a value check admits. The
    gate inspects the value, and the value here is legal, which is precisely
    the point: the *provenance* AST detector above is what names the defect,
    while the gate is what guarantees no wrong value of ANY provenance crosses.
    Both are needed and neither subsumes the other."""
    from tests.membership_gate import NonMemberValue, assert_result_fields, field_member_sets

    members = field_member_sets()
    # A genuinely non-member value through the same local-variable shape.
    with pytest.raises(NonMemberValue):
        assert_result_fields(
            {"classification": "not_a_real_member"},
            where="http.http_strategy_result",
            members=members,
        )
