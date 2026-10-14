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

    A call to the check has to be attributed to the class it is written in, and
    a nested function inside that class is still that class's: the whole point
    of the guard is that the check runs at construction, which is a property of
    the class, not of the function that happens to spell the call.
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

    A second caller is a second place the construction check can be re-spelled,
    which is the shape this consolidation removed: the check was one function
    reached through three identical trampolines, and a class that re-spelled it
    would be indistinguishable from one that inherited it.
    """
    THE_LITERAL_FIELD_CHECK_IS_CALLED_BY_ONE_OWNER.scan()


def test_literal_check_owner_detector_bites():
    """The owner guard fires on a re-spelled trampoline and admits the base.

    The first shape is the pre-consolidation one -- a class calling the check for
    itself -- and the second is the class that is allowed to. The third is the
    shape the guard must NOT fire on: a *different* module's function that
    happens to share the name, resolved by import bindings rather than by text.
    """
    THE_LITERAL_FIELD_CHECK_IS_CALLED_BY_ONE_OWNER.check_bites()


def test_every_taxonomy_literal_dataclass_inherits_the_construction_check():
    """A dataclass that declares a taxonomy ``Literal`` field inherits the check.

    The half of the property the owner guard cannot see: an *omission* rather
    than a duplicate. Which dataclasses qualify is derived from the taxonomy's
    own declaration -- ``LITERAL_FIELDS`` for the field names and
    ``literal_members`` for whether the annotation is really a taxonomy alias --
    and the classes are enumerated by walking ``app/models``, so a result
    dataclass added tomorrow is covered the day it lands and nothing here is a
    hand-kept class list. A field merely *named* like a taxonomy field but
    annotated ``str`` is not one of these and is not reported.

    Splitting the declarations into one file per type used to narrow this: it
    could only see the classes that shared a module with the aliases, and the
    docstring said so and called the owner guard above the tree-wide half. The
    package made that gap disappear -- the aliases and the dataclasses now share
    a HOME rather than a file -- so this guard is the whole property and the
    caveat it used to carry is what makes it worth keeping the enumeration
    derived: ``model_index`` finds every declared class by walking the package.
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
