"""Literal field checks have one owner; literal dataclasses inherit it."""

from __future__ import annotations

import ast
import pathlib

from tests.chokepoint_walks import SANCTIONED, call_nodes, offender_at, resolved_call_name
from tests.rules import Bites, Rule

# It is now inherited from ``models.LiteralChecked``, so the check is part of the class rather than something
# it remembers to ask for -- and "inherited" is a claim the tree can check, which is what these


def _literal_check_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Any call to ``check_literal_fields`` outside its one owning ``__post_init__``."""
    offenders: list[str] = []
    for node in call_nodes(tree):
        if resolved_call_name(tree, node) != "app.models.literal_checked.check_literal_fields":
            continue
        owner = _enclosing_class(tree, node)
        if owner is not None and owner.name == "LiteralChecked":
            continue
        offenders.append(offender_at(path, node, "literal-check-called-outside-its-owner"))
    return offenders


def _enclosing_class(tree: ast.AST, node: ast.AST) -> ast.ClassDef | None:
    """The innermost ``class`` body containing ``node``, or None.

    A call is attributed to the class it is written in (even from a nested
    function), because construction is a property of the class, not the function.
    """
    found: list[ast.ClassDef | None] = []

    def visit(current: ast.AST, enclosing: tuple[ast.ClassDef, ...]) -> bool:
        for child in ast.iter_child_nodes(current):
            if child is node:
                found.append(enclosing[-1] if enclosing else None)
                return True
            nested = enclosing + (child,) if isinstance(child, ast.ClassDef) else enclosing
            if visit(child, nested):
                return True
        return False

    visit(tree, ())
    return found[0] if found else None


THE_LITERAL_FIELD_CHECK_IS_CALLED_BY_ONE_OWNER = Rule(
    name="the-literal-field-check-is-called-by-one-owner",
    find=_literal_check_offenders,
    summary=(
        "check_literal_fields is called outside models.LiteralChecked, so the"
        " construction-time taxonomy check exists in more than one place"
    ),
    excludes=SANCTIONED,
    bite=lambda: Bites(
        "from app.models.literal_checked import check_literal_fields\n"
        "@dataclass(frozen=True)\n"
        "class CrawlResult:\n"
        "    status: str\n"
        "    def __post_init__(self) -> None:\n"
        "        check_literal_fields(self)\n",
        "literal-check-called-outside-its-owner",
    ),
    clean=lambda: (
        # The class that is allowed to call it: the owning base class itself.
        "from app.models.literal_checked import check_literal_fields\n"
        "class LiteralChecked:\n"
        "    def __post_init__(self) -> None:\n"
        "        check_literal_fields(self)\n",
        # A module that never mentions the check is nobody else's business.
        "from app.models.crawl_result import CrawlResult\n"
        "def build() -> CrawlResult:\n    return CrawlResult()\n",
    ),
)


def test_the_literal_field_check_is_called_by_one_owner():
    """One caller for ``check_literal_fields``, and it is the owning base class.

    A second caller is a second place the check can be re-spelled -- the shape
    this consolidation removed (three trampolines into one function).
    """
    THE_LITERAL_FIELD_CHECK_IS_CALLED_BY_ONE_OWNER.scan()


def test_every_taxonomy_literal_dataclass_inherits_the_construction_check():
    """A dataclass that declares a taxonomy ``Literal`` field inherits the check.

    The half the owner guard cannot see is an *omission*: qualifying classes are
    derived from the taxonomy's own declaration and ``model_index``.
    """
    import typing
    from dataclasses import is_dataclass

    from app.models.literal_checked import LITERAL_FIELDS, LiteralChecked
    from app.models.taxonomy import literal_members
    from tests.model_index import declared_types

    checked: dict[str, list[str]] = {}
    for name, cls in sorted(declared_types().items()):
        if not (isinstance(cls, type) and is_dataclass(cls)):
            continue
        named = [field for field in cls.__dataclass_fields__ if field in LITERAL_FIELDS]
        if not named:
            continue
        hints = typing.get_type_hints(cls)
        fields = [field for field in named if literal_members(hints.get(field))]
        if not fields:
            continue
        checked[name] = fields
        assert issubclass(cls, LiteralChecked), (
            f"{name} declares the taxonomy Literal field(s) {sorted(fields)} but no"
            " construction check; inherit LiteralChecked so a non-member is"
            " rejected when the value is built rather than at the wire"
        )
    assert checked, (
        "no taxonomy Literal field was enumerated, so the sweep is a no-op:"
        f" LITERAL_FIELDS={sorted(LITERAL_FIELDS)}"
    )
    # CrawlResult is deliberately ABSENT.
    assert set(checked) == {"CrawlStrategyAttempt", "EngineCrawlResult"}, sorted(checked)
