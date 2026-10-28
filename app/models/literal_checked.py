"""The construction-time taxonomy check, inherited rather than re-spelled: a
result dataclass added tomorrow would otherwise validate nothing. Subclasses
adding ``__post_init__`` must call ``super().__post_init__()``.
"""

from __future__ import annotations

from app.models.taxonomy import (
    Classification,
    CrawlError,
    CrawlStrategyName,
    literal_members,
)
from app.vocabulary.taxonomy import ResultStatus, status_agrees_with_classification

# Kept next to the Literal definitions because the check and the types must move together,
# and public because the taxonomy gate reads it rather than re-deriving a second copy.
LITERAL_FIELDS = {
    "status": set(literal_members(ResultStatus)),
    "classification": set(literal_members(Classification)),
    "selected_strategy": set(literal_members(CrawlStrategyName)),
    "strategy": set(literal_members(CrawlStrategyName)),
    "error": set(literal_members(CrawlError)),
}


def check_literal_fields(obj: object) -> None:
    """Reject a non-member value on any Literal field of a result dataclass --
    the construction-time chokepoint, so a result built anywhere cannot carry an
    out-of-taxonomy value; provenance stays with the AST detector.
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


def check_taxonomy_agreement(obj: object) -> None:
    """Reject a ``(status, classification)`` pair the taxonomy does not allow --
    the per-field check alone would accept a real member beside a disagreeing
    classification. Raising makes the disagreeing pair unrepresentable.
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
