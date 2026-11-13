"""Both directions of a result's wire format, derived from the dataclass whose
key set is spelled ONLY there: to_dict follows dataclasses.fields() and
from_wire reads the same annotations, so a changed field moves both at once.
"""

from __future__ import annotations

import types
from collections.abc import Callable, Mapping
from dataclasses import MISSING, fields, is_dataclass
from typing import Union, cast, get_args, get_origin, get_type_hints

from safe_url.host_policy import HostPolicy
from safe_url.normalized_url import NormalizedUrl

from crawlutron.models.freezable_record import FreezableRecord
from crawlutron.models.json_value import JsonValue


def to_dict(result: FreezableRecord) -> dict[str, JsonValue]:
    """Return a JSON-serializable dict for any frozen-dataclass crawl result, WITHOUT
    deepcopying leaf values (heavy fields are shared by reference; only nested
    dataclasses become dicts). elapsed_ms is a property, so fields() -- this walk -- omits it.
    """
    return cast(dict[str, JsonValue], _to_jsonable(result))


def _to_jsonable(value: object) -> JsonValue:
    return _to_jsonable_impl(value, set())


def _entered(value: object, seen: set[int]) -> set[int] | None:
    """The seen-set to recurse into ``value`` with, or None if it is already on the
    path. Only mutable containers can cycle, so primitives skip it; this is the
    shared path every ``_to_jsonable_impl`` arm descends through.
    """
    oid = id(value)
    if oid in seen:
        return None
    return seen | {oid}


def _build_guarded(
    value: object, seen: set[int], build: Callable[[object, set[int]], JsonValue]
) -> JsonValue | None:
    """Register ``value`` on the path, then let ``build`` render its children.
    ``build`` is never called for a value already on the path -- that call returns
    None instead, breaking a cyclic reference rather than following it.
    """
    entered = _entered(value, seen)
    if entered is None:
        return None
    return build(value, entered)


def _render_dataclass(node: object, path: set[int]) -> JsonValue:
    """One dataclass rendered field-by-field, the dataclass arm of the walk."""
    if is_dataclass(node) and not isinstance(node, type):
        return {f.name: _to_jsonable_impl(getattr(node, f.name), path) for f in fields(node)}
    # Unreachable from the dispatch that guards the arm; a non-dataclass here is
    # a caller bug, and returning it unchanged would hide the shape break.
    raise TypeError(f"_render_dataclass received a non-dataclass: {type(node).__name__}")


def _to_jsonable_impl(value: object, seen: set[int]) -> JsonValue:
    # Every arm below descends through _build_guarded, so the cycle check cannot be
    # forgotten by a new one; primitives skip it, since they cannot cycle.
    if isinstance(value, NormalizedUrl):
        return value.raw
    if is_dataclass(value) and not isinstance(value, type):
        return _build_guarded(value, seen, _render_dataclass)
    if isinstance(value, Mapping):
        return _build_guarded(
            value,
            seen,
            lambda node, path: {
                key: _to_jsonable_impl(item, path)
                for key, item in cast(Mapping[str, JsonValue], node).items()
            },
        )
    if isinstance(value, (list, tuple)):
        return _build_guarded(
            value,
            seen,
            lambda node, path: [_to_jsonable_impl(item, path) for item in cast(list[JsonValue], node)],
        )
    # Leaf value: it is JSON by construction (all dataclass fields are JSON-serializable).
    return cast(JsonValue, value)


def from_wire[ResultT: object](
    cls: type[ResultT], payload: Mapping[str, object], *, policy: HostPolicy
) -> ResultT:
    """Rebuild a result dataclass from the record :func:`to_dict` wrote, the inverse
    of one declaration. ``policy`` re-admits every NormalizedUrl, so a row written
    under one SSRF posture can't come back under another. A missing required field raises.
    """
    # pyright cannot call a value typed as a TypeVar bound to the class parameter. What
    # comes back IS cls, built from its own fields, with __post_init__ already validating.
    return cast(ResultT, _rebuild_dataclass(cls, payload, policy=policy))


def _rebuild_value(annotation: object, value: object, *, policy: HostPolicy) -> object:
    """One value from a serialized record, put back to its declared shape -- only
    structure is rebuilt (nested dataclasses, lists of them). Taxonomy members
    are NOT re-checked here: the real constructor does that in LiteralChecked.
    """
    if annotation is NormalizedUrl:
        # The wire value for a NormalizedUrl is its raw string.
        return NormalizedUrl(cast(str, value), policy=policy)
    if is_dataclass(annotation) and isinstance(annotation, type):
        return _rebuild_dataclass(annotation, value, policy=policy) if isinstance(value, Mapping) else value
    origin = get_origin(annotation)
    if origin is list and isinstance(value, list):
        args = get_args(annotation)
        (item,) = args if len(args) == 1 else (object,)
        return [_rebuild_value(item, element, policy=policy) for element in value]
    if origin is tuple and isinstance(value, (list, tuple)):
        args = get_args(annotation)
        if len(args) == 2 and args[1] is Ellipsis:
            (item,) = (args[0],)
            return tuple(_rebuild_value(item, element, policy=policy) for element in value)
        return value
    if origin in (Union, types.UnionType) and value is not None:
        members = [member for member in get_args(annotation) if member is not type(None)]
        if len(members) == 1:
            return _rebuild_value(members[0], value, policy=policy)
    return value


def _rebuild_dataclass(cls: type, payload: Mapping[str, object], *, policy: HostPolicy) -> object:
    """The walk itself, untyped because the recursion cannot be.
    from_wire is the typed door; ``is_dataclass`` narrows to a class object
    without a type argument, so the recursive arm calls back into here.
    """
    annotations = get_type_hints(cls)
    values: dict[str, object] = {}
    for declared in fields(cls):
        if declared.name not in payload:
            if declared.default is MISSING and declared.default_factory is MISSING:
                raise TypeError(
                    f"{cls.__name__} requires {declared.name!r}, which the serialized record "
                    f"does not carry; the record was not written by to_dict for this class"
                )
            continue
        values[declared.name] = _rebuild_value(
            annotations.get(declared.name), payload[declared.name], policy=policy
        )
    return cls(**values)
