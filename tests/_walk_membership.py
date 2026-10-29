"""Membership checks: literals, models and vocabularies."""

import ast
import pathlib
from collections.abc import Container, Iterable, Iterator
from functools import lru_cache
from types import ModuleType

from tests._walk_primitives import lineno, offender
from tests._walk_resolution import call_keyword_arguments


@lru_cache(maxsize=1)
def _model_modules() -> tuple[ModuleType, ...]:
    """Every module in ``crawlutron.models``, derived rather than enumerated.

    A stale candidate list resolves a taxonomy to nothing -- which approves
    every literal rather than failing."""
    import importlib
    import pkgutil

    package = importlib.import_module("crawlutron.models")
    return tuple(
        importlib.import_module(f"crawlutron.models.{info.name}")
        for info in pkgutil.iter_modules(package.__path__)
    )


def _vocabulary_modules() -> tuple[ModuleType, ...]:
    """Every topic module in ``crawlutron.vocabulary``, derived the same way.

    A taxonomy name is looked up across the split-by-topic package; a stale or
    single-module lookup approves every literal."""
    import importlib
    import pkgutil

    package = importlib.import_module("crawlutron.vocabulary")
    return tuple(
        importlib.import_module(f"crawlutron.vocabulary.{info.name}")
        for info in pkgutil.iter_modules(package.__path__)
    )


def _spelled_non_member(value: ast.expr, members: Container[str]) -> str | None:
    """The string literal at ``value`` when it is not a member, else ``None``.

    Non-literals pass without comment (``None`` needs no special case); a
    dropped ``isinstance`` guard reports variables as if they were literals."""
    if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
        return None
    return None if value.value in members else value.value


def non_member_literals(
    candidates: Iterable[tuple[ast.Call, str, ast.expr, Container[str]]],
) -> Iterator[tuple[ast.Call, str, str]]:
    """``(node, field, value, members)`` tuples narrowed to spelled non-members.

    ``node`` is an ``ast.Call`` because every caller feeds the call-keyword
    walk's output; widening it would cost each caller the callee and line."""
    for node, field, value, members in candidates:
        spelled = _spelled_non_member(value, members)
        if spelled is not None:
            yield node, field, spelled


def literal_keyword_membership_offenders(
    tree: ast.AST,
    path: pathlib.Path,
    *,
    members,
    function_names: str | frozenset[str],
    keyword_names: str | tuple[str, ...],
    label: str,
    type_name: str,
    advice: str,
) -> list[str]:
    """A literal passed to ``function_names(**keyword_names)`` that is not a member.

    The shared seven lines of the enum-member detectors, ``members`` from the
    live type; a non-string literal is never reported (``None``)."""
    return [
        offender(
            path,
            lineno(node),
            f"{label}={value!r} is not a {type_name} member; {advice}",
        )
        for node, _field, value in non_member_literals(
            (node, str(kw.arg), kw.value, members)
            for node, kw in call_keyword_arguments(
                tree,
                function_names=function_names,
                keyword_names=keyword_names,
            )
        )
    ]


def real_members(type_name: str) -> frozenset[str]:
    """The live member set of a taxonomy named by string.

    ``str``-Enum: values; ``Literal`` alias: ``literal_members`` (total over
    the union); a non-taxonomy raises ``LookupError`` -- never silence."""
    from crawlutron.models import taxonomy

    for module in (taxonomy, *_model_modules(), *_vocabulary_modules()):
        owner = getattr(module, type_name, None)
        if owner is not None:
            break
    else:
        raise LookupError(
            f"{type_name} is not a taxonomy anywhere in crawlutron/models or crawlutron/vocabulary"
        )
    if hasattr(owner, "__members__"):
        return frozenset(member.value for member in owner)
    return taxonomy.literal_members(owner)
