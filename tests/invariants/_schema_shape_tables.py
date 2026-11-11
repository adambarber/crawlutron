"""Shared derivation for the OpenAPI schema-shape gate: every component
schema the served OpenAPI declares, projected against its SDK mirror.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from pydantic import BaseModel

from tests._app_factory import service_app
from tests.invariants._facade_tables import _json_body_ref
from tests.invariants._schema_shape import Shape, annotation_shape, schema_shape

#: The served spec is JSON at the boundary; the gate reads only the keys below.
#: Cast provenance: openapi() returns Any, narrowed by the reads that follow.
_Spec = dict[str, Any]
_FieldShapes = dict[str, Shape]
_WireShapeMap = dict[str, dict[str, object]]

#: SDK mirror classes that name a differently titled wire schema.
#: Requests forbid extra fields; responses allow them (the docstring law).
_SCHEMA_NAME_MAP = {"CrawlResponse": "UnifiedCrawlResponse", "ErrorEnvelope": "ErrorResponse"}
_WIRE_METHODS = frozenset({"get", "post", "put", "patch", "delete"})


@dataclass(frozen=True)
class SchemaShapeRow:
    """One SDK mirror class pinned against its served OpenAPI schema."""

    sdk_name: str
    wire_name: str
    role: str
    sdk_extra: str
    wire_extra: str
    sdk_fields: frozenset[str]
    wire_fields: frozenset[str]
    sdk_required: frozenset[str]
    wire_required: frozenset[str]
    sdk_vocab: dict[str, frozenset[str]]
    wire_vocab: dict[str, frozenset[str]]
    sdk_shapes: _FieldShapes
    wire_shapes: _FieldShapes


def _schemas() -> _WireShapeMap:
    """The served OpenAPI component schemas (single derivation point)."""
    spec: _Spec = service_app().openapi()
    return spec["components"]["schemas"]


def _loose_dict(shape: Shape) -> Shape:
    """Collapse dict/model-typed fields: both project as the wire ``object``."""
    if shape.type_set in (frozenset({"dict"}), frozenset({"object"})):
        return Shape(mark="object")
    return shape


def _request_schema_names(spec: _Spec) -> frozenset[str]:
    refs: set[str] = set()
    for item in spec["paths"].values():
        for method, op in item.items():
            if method not in _WIRE_METHODS:
                continue
            ref = _json_body_ref(op)
            if ref is not None:
                refs.add(ref)
    return frozenset(refs)


def _unserved_row(sdk_name: str, cls: type[BaseModel], wire_name: str) -> SchemaShapeRow:
    """A row for an SDK class the served OpenAPI does not declare by name."""
    return SchemaShapeRow(
        sdk_name=sdk_name,
        wire_name=wire_name,
        role="unserved",
        sdk_extra=str(cls.model_config.get("extra")),
        wire_extra="",
        sdk_fields=frozenset(cls.model_fields),
        wire_fields=frozenset(),
        sdk_required=_required_fields(cls),
        wire_required=frozenset(),
        sdk_vocab={},
        wire_vocab={},
        sdk_shapes={},
        wire_shapes={},
    )


def _properties(schema: dict[str, object]) -> dict[str, object]:
    """One schema's properties projection, narrowed from the JSON boundary."""
    properties = schema.get("properties", {})
    assert isinstance(properties, dict)
    return properties


def _string_list(schema: dict[str, object], key: str) -> frozenset[str]:
    members = schema.get(key, [])
    assert isinstance(members, list)
    return frozenset(str(m) for m in members)


def _served_shapes(
    cls: type[BaseModel], schema: dict[str, object], schemas: _WireShapeMap
) -> tuple[dict[str, frozenset[str]], dict[str, frozenset[str]], _FieldShapes, _FieldShapes]:
    """Vocabulary and type projections for each shared field of one row."""
    sdk_vocab: dict[str, frozenset[str]] = {}
    wire_vocab: dict[str, frozenset[str]] = {}
    sdk_shapes: _FieldShapes = {}
    wire_shapes: _FieldShapes = {}
    for field, prop in _properties(schema).items():
        assert isinstance(prop, dict)
        sdk_shape = annotation_shape(cls.model_fields[field].annotation)
        wire_shape = schema_shape(prop, schemas)
        if sdk_shape.vocabulary is not None:
            sdk_vocab[field] = sdk_shape.vocabulary
        if wire_shape.vocabulary is not None:
            wire_vocab[field] = wire_shape.vocabulary
        sdk_shapes[field] = _loose_dict(sdk_shape)
        wire_shapes[field] = _loose_dict(wire_shape)
    return sdk_vocab, wire_vocab, sdk_shapes, wire_shapes


def _served_row(
    sdk_name: str,
    cls: type[BaseModel],
    wire_name: str,
    schema: dict[str, object],
    schemas: _WireShapeMap,
    role: str,
) -> SchemaShapeRow:
    """A row pinning one served schema against its SDK mirror class."""
    sdk_vocab, wire_vocab, sdk_shapes, wire_shapes = _served_shapes(cls, schema, schemas)
    return SchemaShapeRow(
        sdk_name=sdk_name,
        wire_name=wire_name,
        role=role,
        sdk_extra=str(cls.model_config.get("extra")),
        wire_extra=str(schema.get("additionalProperties", "absent")),
        sdk_fields=frozenset(cls.model_fields),
        wire_fields=frozenset(_properties(schema)),
        sdk_required=_required_fields(cls),
        wire_required=_string_list(schema, "required"),
        sdk_vocab=sdk_vocab,
        wire_vocab=wire_vocab,
        sdk_shapes=sdk_shapes,
        wire_shapes=wire_shapes,
    )


def _required_fields(cls: type[BaseModel]) -> frozenset[str]:
    return frozenset(f for f, fi in cls.model_fields.items() if fi.is_required())


def schema_shape_rows() -> tuple[SchemaShapeRow, ...]:
    """Derive one row per SDK mirror class from the served OpenAPI spec."""
    import crawlutron_sdk.models as sdk

    spec: _Spec = service_app().openapi()
    schemas = _schemas()
    request_names = _request_schema_names(spec)
    rows: list[SchemaShapeRow] = []
    for sdk_name in sorted(sdk.__all__):
        cls = cast(type[BaseModel], getattr(sdk, sdk_name))
        wire_name = _SCHEMA_NAME_MAP.get(sdk_name, sdk_name)
        schema = schemas.get(wire_name)
        if schema is None:
            rows.append(_unserved_row(sdk_name, cls, wire_name))
            continue
        role = "request" if wire_name in request_names else "response"
        rows.append(_served_row(sdk_name, cls, wire_name, schema, schemas, role))
    return tuple(rows)
