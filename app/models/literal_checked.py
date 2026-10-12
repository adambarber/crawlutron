"""The construction-time taxonomy check, inherited rather than re-spelled.

Every result dataclass declares fields whose annotation is a taxonomy
``Literal``, so every one of them has to reject a non-member at construction.
That check is one function, but the ``__post_init__`` that calls it was a
two-line trampoline repeated on each class, which is a second place to forget: a
result dataclass added tomorrow would validate nothing and read exactly like the
ones that do. Inheriting makes the check part of the class rather than something
it remembers to ask for, and it leaves one spelling of it in the tree for a guard
to point at.

Subclassing note: defining ``__post_init__`` on a subclass would *override* this
one, so a result dataclass that grows a post-init of its own has to call
``super().__post_init__()``. The taxonomy check is the reason the base exists, so
losing it silently is the one outcome that must not happen.
"""

from __future__ import annotations

from typing import Any

from app.models.taxonomy import (
    Classification,
    CrawlError,
    CrawlStrategyName,
    literal_members,
)
from app.vocabulary.taxonomy import ResultStatus, status_agrees_with_classification

# Fields whose declared type is a Literal: a value that is not a member is a
# shape lie, so the dataclasses validate them at construction. Kept here next to
# the Literal definitions because the check and the types must move together,
# and PUBLIC rather than private because the taxonomy gate reads it to decide
# which dataclasses qualify -- a gate that had to re-derive this map would be a
# second copy of it.
LITERAL_FIELDS = {
    "status": set(literal_members(ResultStatus)),
    "classification": set(literal_members(Classification)),
    "selected_strategy": set(literal_members(CrawlStrategyName)),
    "strategy": set(literal_members(CrawlStrategyName)),
    "error": set(literal_members(CrawlError)),
}


def check_literal_fields(obj: Any) -> None:
    """Reject a non-member value on any Literal field of a result dataclass.

    This is the construction-time chokepoint: ``engine_result`` builds most
    results, but a dataclass built anywhere else (engine.py, strategies, tests)
    previously escaped the only runtime guard. Checking in ``__post_init__``
    closes the class by construction -- no future direct build can carry an
    out-of-taxonomy value -- for the cost of one set lookup per field.
    Namespace/provenance (a member *value* arriving from the wrong constant) is
    not covered here and stays with the AST detector.
    """
    for name, allowed in LITERAL_FIELDS.items():
        if name not in obj.__dict__:
            continue
        value = obj.__dict__[name]
        if value is not None and value not in allowed:
            raise ValueError(
                f"{type(obj).__name__}.{name}={value!r} is not a member of its "
                f"declared Literal ({len(allowed)} allowed)"
            )


def check_taxonomy_agreement(obj: Any) -> None:
    """Reject a ``(status, classification)`` pair the taxonomy does not allow.

    :func:`check_literal_fields` knows each field independently, so it accepts a
    ``status`` that is a real member beside a ``classification`` whose agreed
    status is a different one. Both axes are free parameters of every result
    constructor, which is exactly the shape that lets the wire assert one status
    while the taxonomy re-derives another for the same label. Raising here makes
    the disagreeing pair unrepresentable rather than merely undesirable, and it
    does so for every record that carries both axes, whether or not a
    construction chokepoint remembered to check.

    Records without both fields are left alone: a worker envelope that never got
    as far as an outcome carries neither.
    """
    values = obj.__dict__
    classification = values.get("classification")
    status = values.get("status")
    if classification is None or status is None:
        return
    if not status_agrees_with_classification(classification, status):
        raise ValueError(
            f"{type(obj).__name__}: status {status!r} disagrees with the taxonomy status for "
            f"classification {classification!r}"
        )


class LiteralChecked:
    """Base for a dataclass whose fields carry taxonomy ``Literal`` annotations."""

    def __post_init__(self) -> None:
        check_literal_fields(self)
        check_taxonomy_agreement(self)
