"""Both directions of a result's wire format, derived from the dataclass.

A result's key set is spelled by its dataclass and NOWHERE ELSE. Both halves walk
that same class -- ``to_dict`` follows ``dataclasses.fields()`` and ``from_wire``
reads the same field annotations -- so a field renamed, added or dropped moves
both directions at once.

That is the point. The spelling this replaced was a ``TypedDict`` mirroring every
field, which was a second copy of the key set that nothing but a detector held in
step, and a consumer indexing into it got no build failure when the two
disagreed. Two functions rather than one with a cast inside the loop: the
unchecked width is confined to the body that cannot be typed, and the signature a
caller reads is the one that carries the contract.
"""

from __future__ import annotations

import types
from collections.abc import Callable, Mapping
from dataclasses import MISSING, fields, is_dataclass
from typing import Any, TypeVar, Union, cast, get_args, get_origin, get_type_hints

from app.models.crawl_result import CrawlResult, EngineCrawlResult
from app.models.host_policy import HostPolicy
from app.models.normalized_url import NormalizedUrl

_ResultT = TypeVar("_ResultT", bound=Any)


def to_dict(result: EngineCrawlResult | CrawlResult) -> dict[str, Any]:
    """Return a JSON-serializable dict for any frozen-dataclass crawl result.

    Unlike ``dataclasses.asdict``, this does NOT ``deepcopy`` leaf values. The
    heavy fields (``html``/``text`` strings, ``metadata``/``diagnostics``
    dicts) are shared by reference, and only nested dataclasses (e.g.
    ``DiscoveredLink``) are converted to dicts -- so the output stays
    JSON-serializable without paying asdict's full recursive deep-copy cost on
    every field of every result in a large batch.

    Note what is NOT here: ``elapsed_ms`` is a property on
    :class:`EngineCrawlResult`, and ``dataclasses.fields()`` -- which the walk
    below follows -- does not include properties, so the engine's spill carries
    no elapsed time. :func:`from_wire` has the same blind spot, which is why it
    is derived rather than written twice: both directions read the one
    declaration of what a result is.
    """
    return _to_jsonable(result)


def _to_jsonable(value: Any) -> Any:
    return _to_jsonable_impl(value, set())


def _entered(value: Any, seen: set[int]) -> set[int] | None:
    """The seen-set to recurse into ``value`` with, or None if it is already on the path.

    Only mutable containers can take part in a reference cycle, which is why the
    primitive types skip it entirely. :func:`_build_guarded` is its only caller:
    a container kind added to ``_to_jsonable_impl`` arrives as a new
    ``isinstance`` arm, and nothing about adding one makes this check mandatory
    there, so the check belongs on the arm's shared path rather than in each arm.
    """
    oid = id(value)
    if oid in seen:
        return None
    return seen | {oid}


def _build_guarded(value: Any, seen: set[int], build: Callable[[Any, set[int]], Any]) -> Any:
    """Register ``value`` on the path, then let ``build`` render its children.

    ``build`` is given the value and the descended path set, and is never called
    for a value already on that path -- that call returns None instead, which is
    how a cyclic reference is broken rather than followed.
    """
    entered = _entered(value, seen)
    if entered is None:
        return None
    return build(value, entered)


def _to_jsonable_impl(value: Any, seen: set[int]) -> Any:
    # Cyclic graphs (e.g. a DiscoveredLink whose parent/children point back to
    # an ancestor) recurse forever without a check, and a RecursionError at
    # serialization time is far from the code that caused it. Every arm below
    # descends through _build_guarded, so the check cannot be forgotten by a
    # new one. Primitives skip it: they cannot cycle, and small strings may be
    # interned and share an id across the tree, so flagging them would truncate
    # valid results.
    if isinstance(value, NormalizedUrl):
        return value.raw
    if is_dataclass(value) and not isinstance(value, type):
        return _build_guarded(
            value,
            seen,
            lambda node, path: {f.name: _to_jsonable_impl(getattr(node, f.name), path) for f in fields(node)},
        )
    if isinstance(value, dict):
        return _build_guarded(
            value, seen, lambda node, path: {key: _to_jsonable_impl(item, path) for key, item in node.items()}
        )
    if isinstance(value, (list, tuple)):
        return _build_guarded(
            value, seen, lambda node, path: [_to_jsonable_impl(item, path) for item in node]
        )
    return value


def from_wire[ResultT: Any](cls: type[ResultT], payload: Mapping[str, Any], *, policy: HostPolicy) -> ResultT:
    """Rebuild a result dataclass from the record :func:`to_dict` wrote.

    policy`` re-admits every ``NormalizedUrl`` the record carries: a spill row
    is the canonical string it wrote, and rebuilding re-runs the constructor,
    so a result written under one SSRF posture cannot come back under another.
    That makes the read a consistency check rather than a blind trust of the file.

    The inverse half of ONE declaration, described in this module's preamble.

    Absence is asymmetric on purpose. :func:`to_dict` writes every field, so a
    field missing from the payload means the writer was not the serializer; a
    defaulted field can answer that with its own default, and a required one
    cannot, so it raises. That is what turns a producer that stopped writing a
    field into a loud failure here rather than a response quietly missing it.
    """
    # The one cast in this module, and it is a limitation of the checker rather
    # than a gap in the contract: pyright cannot call a value whose type is a
    # TypeVar bound to the class parameter. What comes back IS `cls`, built from
    # its own fields, with ``__post_init__`` having already validated them.
    return cast(ResultT, _rebuild_dataclass(cls, payload, policy=policy))


def _rebuild_value(annotation: Any, value: Any, *, policy: HostPolicy) -> Any:
    """One value from a serialized record, put back to its declared shape.

    Only structure is rebuilt: a nested dataclass becomes a dataclass again, and
    a list of them becomes a list of dataclasses. Scalars, ``dict[str, str]``
    payloads and open ``dict[str, Any]`` blocks are already the values their
    annotations declare, so they pass through untouched. Taxonomy members are
    deliberately NOT checked here -- the real constructor does that in
    ``LiteralChecked.__post_init__``, so this adds no second spelling of the
    validation.
    """
    if annotation is NormalizedUrl:
        return NormalizedUrl(value, policy=policy)
    if is_dataclass(annotation):
        return _rebuild_dataclass(annotation, value, policy=policy) if isinstance(value, Mapping) else value
    origin = get_origin(annotation)
    if origin is list and isinstance(value, list):
        args = get_args(annotation)
        (item,) = args if len(args) == 1 else (Any,)
        return [_rebuild_value(item, element, policy=policy) for element in value]
    if origin in (Union, types.UnionType) and value is not None:
        members = [member for member in get_args(annotation) if member is not type(None)]
        if len(members) == 1:
            return _rebuild_value(members[0], value, policy=policy)
    return value


def _rebuild_dataclass(cls: Any, payload: Mapping[str, Any], *, policy: HostPolicy) -> Any:
    """The walk itself, untyped because the recursion cannot be.

    :func:`from_wire` is the typed door; this is what it calls, and the recursive
    arm calls back into here because ``is_dataclass`` narrows to a class object
    without a type argument, which no generic class-parameter signature accepts.
    The argument for the split is in this module's preamble.
    """
    annotations = get_type_hints(cls)
    values: dict[str, Any] = {}
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
