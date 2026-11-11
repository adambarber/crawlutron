"""Type-identity helpers: one shape vocabulary shared by both pin clauses.

The SDK annotation and the OpenAPI projection each reduce to a ``Shape``:
a literal vocabulary, an ``object`` mark, or a wire type-set. A vocabulary
equals a vocabulary; type-sets must be wire-compatible.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal, Union, get_args, get_origin

TYPE_MARKS = frozenset({"object", "dict", "list"})


@dataclass(frozen=True)
class Shape:
    """One side's reduced shape: a vocabulary, a mark, or a wire type-set."""

    vocabulary: frozenset[str] | None = None
    mark: str | None = None
    type_set: frozenset[str] = frozenset()


def _scalar_mark(python_type: object) -> frozenset[str]:
    marks: dict[object, str] = {
        str: "string",
        bool: "boolean",
        int: "integer",
        float: "number",
        dict: "dict",
        list: "list",
    }
    mark = marks.get(python_type)
    return frozenset({mark}) if mark else frozenset()


def _merge_shapes(parts: list[Shape]) -> Shape:
    """Union reduction: any literal member makes the whole side a vocabulary."""
    vocabs = [p.vocabulary for p in parts if p.vocabulary is not None]
    if vocabs:
        return Shape(vocabulary=frozenset().union(*vocabs))
    merged: frozenset[str] = frozenset()
    for part in parts:
        merged |= part.type_set
    return Shape(type_set=merged)


def annotation_shape(annotation: object) -> Shape:
    """Reduce a pydantic annotation to a Shape."""
    if get_origin(annotation) is Annotated:
        return annotation_shape(get_args(annotation)[0])
    origin = get_origin(annotation)
    if origin is Union:
        return _merge_shapes([annotation_shape(a) for a in get_args(annotation) if a is not type(None)])
    if origin is Literal:
        return Shape(vocabulary=frozenset(get_args(annotation)))
    if origin is list:
        return Shape(type_set=frozenset({"list"}))
    if origin is dict:
        return Shape(type_set=frozenset({"dict"}))
    scalar = _scalar_mark(annotation)
    if scalar:
        return Shape(type_set=scalar)
    if annotation is object or annotation is None:
        return Shape(type_set=TYPE_MARKS)
    return Shape(type_set=frozenset({"object"}))


def schema_shape(prop: dict[str, object], schemas: dict[str, dict[str, object]] | None = None) -> Shape:
    """Reduce one OpenAPI property projection to a Shape."""
    schema_map = schemas if schemas is not None else {}
    any_of = prop.get("anyOf")
    if isinstance(any_of, list):
        return _merge_shapes([schema_shape(sub, schema_map) for sub in any_of])
    if "enum" in prop:
        members = prop["enum"]
        assert isinstance(members, list)
        return Shape(vocabulary=frozenset(str(m) for m in members))
    return _schema_ref_or_type(prop, schema_map)


def _schema_ref_or_type(prop: dict[str, object], schema_map: dict[str, dict[str, object]]) -> Shape:
    ref = prop.get("$ref")
    if isinstance(ref, str):
        ref_schema = schema_map.get(ref.rsplit("/", 1)[-1], {})
        members = ref_schema.get("enum")
        if isinstance(members, list):
            return Shape(vocabulary=frozenset(str(m) for m in members))
        return Shape(type_set=frozenset({"object"}))
    wire = prop.get("type")
    mapping = {
        "array": "list",
        "object": "dict",
        "integer": "integer",
        "number": "number",
        "boolean": "boolean",
        "string": "string",
    }
    if wire == "null":
        return Shape()
    if isinstance(wire, str) and wire in mapping:
        return Shape(type_set=frozenset({mapping[wire]}))
    return Shape(type_set=TYPE_MARKS)


def shapes_agree(sdk_shape: Shape, wire_shape: Shape) -> bool:
    """A vocabulary must equal the wire vocabulary; type-sets must intersect."""
    if sdk_shape.vocabulary is not None or wire_shape.vocabulary is not None:
        return sdk_shape.vocabulary == wire_shape.vocabulary
    if sdk_shape.mark is not None or wire_shape.mark is not None:
        return sdk_shape.mark == wire_shape.mark
    return bool(sdk_shape.type_set & wire_shape.type_set)
