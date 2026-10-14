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
    """Every module in ``app.models``, derived rather than enumerated.

    A vocabulary that lives in one of these files is found by reading the file it
    lives in. Naming the candidate modules instead meant the list went stale every
    time a record moved, and a stale list resolves a taxonomy to nothing -- which
    approves every literal rather than failing.
    """
    import importlib
    import pkgutil

    package = importlib.import_module("app.models")
    return tuple(
        importlib.import_module(f"app.models.{info.name}") for info in pkgutil.iter_modules(package.__path__)
    )


def _vocabulary_modules() -> tuple[ModuleType, ...]:
    """Every topic module in ``app.vocabulary``, derived the same way.

    A taxonomy name has to be looked up across the package rather than in one
    module: the vocabulary is split by topic, so ``CrawlClassification`` is in
    ``taxonomy`` and the strategy names are in ``strategies``. A lookup that
    tried a single module would raise ``LookupError`` on a name that exists --
    and, worse, one that tried a stale list would approve every literal.
    """
    import importlib
    import pkgutil

    package = importlib.import_module("app.vocabulary")
    return tuple(
        importlib.import_module(f"app.vocabulary.{info.name}")
        for info in pkgutil.iter_modules(package.__path__)
    )


def _spelled_non_member(value: ast.expr, members: Container[str]) -> str | None:
    """The string literal at ``value`` when it is not a member, else ``None``.

    The two decisions every "a spelled value must be a real member" detector has
    to make before it can report anything, and the two it gets wrong quietly: a
    non-literal is what a correct caller writes and passes without comment (so
    ``None`` is admitted without a special case -- the member set is a set of
    strings and the honest non-member is the one that is spelled), and a member
    is not a defect. Written per detector, the dropped ``isinstance`` guard does
    not fail a test -- it reports a variable as if it were a literal.
    """
    if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
        return None
    return None if value.value in members else value.value


def non_member_literals(
    candidates: Iterable[tuple[ast.Call, str, ast.expr, Container[str]]],
) -> Iterator[tuple[ast.Call, str, str]]:
    """``(node, field, value, members)`` tuples narrowed to spelled non-members.

    The set-valued funnel over :func:`_spelled_non_member`, for the detectors that
    sweep more than one keyword at a time and so cannot fix ``members`` up front
    -- a Literal field's members are keyed by the field name, which is also what
    the offending message has to name. Reporting the value rather than the
    expression is deliberate: the message is about the value, and the only thing
    left for the caller to say is why it is wrong there.

    The node is an ``ast.Call`` because every caller feeds it the output of a
    call-keyword walk, which yields calls. Typing it as ``ast.AST`` widened the
    node on the way through and cost each caller the callee and the line it went
    on to read.
    """
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

    The shared body of the "a spelled value must be a real enum member" detectors
    (result status, crawl strategy, ...). Each of those was the same seven lines
    over :func:`call_keyword_values` with a different message, and a hand-rolled
    copy is exactly where a member could be added to one axis and forgotten on
    the other. ``members`` is read from the live type at test time, never from a
    hand-kept exemption list.

    A non-string literal is never reported, which is what admits ``None`` for
    "no strategy was attempted" without a special case -- see
    :func:`_spelled_non_member`, which owns that decision for every detector here
    and in ``tests/membership_gate.py`` rather than each restating it.
    """
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

    The one answer to "what is a real member of X", for the detectors that guard
    a call site against a literal that is not one. It exists because the
    detectors were taking the set as a keyword: every scan test re-derived it
    and every bites fixture could hand a detector a different answer to the
    question the detector exists to ask, which does not fail -- it approves the
    wrong literals.

    Two spellings, and the split is the load-bearing part rather than a
    convenience. A ``str``-Enum contributes its values, because a caller writes
    ``CrawlStrategy.HTTP_DECLARED_CRAWLER`` and the wire carries ``.value``. A
    ``Literal`` alias goes through ``app.models.taxonomy.literal_members`` rather than
    ``typing.get_args``, because that one is total over a union -- which is what
    lets a taxonomy be *defined* as a union of two others instead of restating
    one of them, and ``get_args`` would hand back the nested aliases instead of
    their members.

    Raises ``LookupError`` for a name that is not a taxonomy. A guard that
    silently resolved to the empty set would approve every literal, which is the
    failure this function exists to make impossible.
    """
    from app.models import taxonomy

    for module in (taxonomy, *_model_modules(), *_vocabulary_modules()):
        owner = getattr(module, type_name, None)
        if owner is not None:
            break
    else:
        raise LookupError(f"{type_name} is not a taxonomy anywhere in app/models or app/vocabulary")
    if hasattr(owner, "__members__"):
        return frozenset(member.value for member in owner)
    return taxonomy.literal_members(owner)
