"""Schema-shape gate: the SDK mirror pinned against the served OpenAPI. Every SDK model class is derived from
``service_app.openapi`` at test time: field sets both directions, type identity, required-ness, and the
per-class extra= policy.
"""

from __future__ import annotations

from tests.invariants._schema_shape import shapes_agree
from tests.invariants._schema_shape_tables import SchemaShapeRow, schema_shape_rows

#: Response models tolerate unknown future fields (models.py docstring law).
_RESPONSE_EXTRA = "allow"
_REQUEST_EXTRA = "forbid"

#: Declared exceptions (parent ruling, #847). WatchCreateRequest: models.py's
#: docstring sanctions extra="allow"; the served schema omits additionalProperties.
EXTRA_POLICY_EXCEPTIONS = {"WatchCreateRequest"}

#: 202 accepted body is not a declared response schema in openapi() — a
#: DERIVATION CEILING, not a clean bill; revisited if derivation widens (#846).
UNSERVED_SDK_CLASSES = {"CrawlAccepted"}


def _rows() -> tuple[SchemaShapeRow, ...]:
    return schema_shape_rows()


def _served_rows() -> list[SchemaShapeRow]:
    """Rows with a served wire schema (unserved rows are a separate clause)."""
    return [row for row in _rows() if row.role != "unserved"]


def test_sdk_field_sets_match_openapi_both_directions():
    """SDK field not in schema, and schema field not in SDK, both red."""
    extra: list[str] = []
    missing: list[str] = []
    for row in _served_rows():
        for field in sorted(row.sdk_fields - row.wire_fields):
            extra.append(f"{row.sdk_name}.{field}")
        for field in sorted(row.wire_fields - row.sdk_fields):
            missing.append(f"{row.sdk_name}.{field}")
    assert not extra, f"SDK fields absent from the served OpenAPI schema: {extra}"
    assert not missing, f"OpenAPI schema fields absent from the SDK mirror: {missing}"


def test_sdk_type_identity_matches_openapi():
    """Literal vocabularies equal the wire enum; base types are wire-compatible."""
    drifts: list[str] = []
    for row in _served_rows():
        for field in sorted(row.sdk_fields & row.wire_fields):
            if field in row.sdk_vocab or field in row.wire_vocab:
                if row.sdk_vocab.get(field) != row.wire_vocab.get(field):
                    drifts.append(
                        f"{row.sdk_name}.{field}: sdk vocab "
                        f"{sorted(row.sdk_vocab.get(field, frozenset()))} != wire vocab "
                        f"{sorted(row.wire_vocab.get(field, frozenset()))}"
                    )
            elif not shapes_agree(row.sdk_shapes[field], row.wire_shapes[field]):
                drifts.append(
                    f"{row.sdk_name}.{field}: sdk types {sorted(row.sdk_shapes[field].type_set)} "
                    f"incompatible with wire types {sorted(row.wire_shapes[field].type_set)}"
                )
    assert not drifts, f"type identity drift between SDK mirror and served OpenAPI: {drifts}"


def test_sdk_requiredness_never_tightens_the_wire():
    """Request models pin required-ness exactly; responses pin only tightening.

    A response mirror LOOSER than the wire is safe (tolerates absent rows); a
    request mirror looser than the wire would accept invalid bodies.
    """
    offenders: list[str] = []
    for row in _served_rows():
        if row.role == "request":
            for field in sorted(row.wire_required ^ row.sdk_required):
                offenders.append(f"{row.sdk_name}.{field}: request required-ness differs from wire")
        else:
            for field in sorted(row.sdk_required - row.wire_required):
                offenders.append(
                    f"{row.sdk_name}.{field}: SDK-required but wire-optional (tightens the contract)"
                )
    assert not offenders, f"required-ness drift between SDK mirror and served OpenAPI: {offenders}"


def test_sdk_extra_policy_requests_forbid_responses_allow():
    """Request mirrors forbid extras; response mirrors allow them (docstring law)."""
    offenders: list[str] = []
    for row in _served_rows():
        expected = _REQUEST_EXTRA if row.role == "request" else _RESPONSE_EXTRA
        if row.sdk_name in EXTRA_POLICY_EXCEPTIONS:
            continue
        if row.sdk_extra != expected:
            offenders.append(f"{row.sdk_name}: extra={row.sdk_extra}, expected {expected}")
    assert not offenders, f"extra= policy drift in the SDK mirror: {offenders}"


def test_every_sdk_mirror_names_a_served_schema():
    """An SDK class with no OpenAPI counterpart (renamed/removed on the wire) reds."""
    orphaned = [
        row.sdk_name for row in _rows() if row.role == "unserved" and row.sdk_name not in UNSERVED_SDK_CLASSES
    ]
    assert not orphaned, f"SDK mirror classes with no served OpenAPI schema: {orphaned}"
