"""The pydantic default statement must sit where a checker can see it: a default written inside ``Field``
reads as "no default", so the model looks required to every constructor call, and a repeated default on
the assignment can drift from it.
"""

from __future__ import annotations

import ast
import pathlib
from collections.abc import Iterator

from tests.chokepoint_walks import (
    app_trees,
    assert_fires_once,
    assert_stays_clean,
    assignments,
    nodes_of,
)


def _annotated_field_calls(annotation: ast.expr | None) -> Iterator[ast.Call]:
    """Every ``Field(...)`` written as metadata inside an ``Annotated[...]``.

    The half the detector was blind to, and here the larger one: the default is
    stated twice, in the slot a checker cannot see.
    """
    if not (isinstance(annotation, ast.Subscript) and isinstance(annotation.value, ast.Name)):
        return
    if annotation.value.id != "Annotated":
        return
    items = annotation.slice.elts if isinstance(annotation.slice, ast.Tuple) else [annotation.slice]
    for item in items:
        if isinstance(item, ast.Call) and isinstance(item.func, ast.Name) and item.func.id == "Field":
            yield item


def _positional_field_default_offenders(tree: ast.Module, path: pathlib.Path | None = None) -> list[str]:
    """``Field(<default>, ...)`` sites, wherever the ``Field`` is written.

    Ellipsis excluded (really-required); ``default_factory=`` excluded
    (a checker resolves it); both spellings swept -- they are one rule.
    """
    found: list[str] = []
    where = f"{path.name}:" if path is not None else ""
    for node, target, value in assignments(nodes_of(tree)):
        candidates: list[ast.Call] = [value] if isinstance(value, ast.Call) else []
        annotation = node.annotation if isinstance(node, ast.AnnAssign) else None
        candidates.extend(_annotated_field_calls(annotation))
        for call in candidates:
            if not (isinstance(call.func, ast.Name) and call.func.id == "Field"):
                continue
            if any(key.arg == "default_factory" for key in call.keywords):
                continue
            if call.args:
                default: ast.expr | None = call.args[0]
            else:
                default = next((key.value for key in call.keywords if key.arg == "default"), None)
            if default is None or (isinstance(default, ast.Constant) and default.value is ...):
                continue
            found.append(
                f"{where}{ast.unparse(target)} declares its default inside Field(); a checker "
                f"reads that slot as no default, so the field looks required to every "
                f"constructor call -- and where the assignment repeats it, the two "
                f"statements of one default can drift. Declare it once, on the "
                f"assignment: Annotated[T, Field(...)] = <default>, or a bare = <default> "
                f"when Field carries nothing else."
            )
    return found


def test_pydantic_defaults_stay_where_a_checker_can_see_them() -> None:
    """Whole-tree, not just api_models.py: any module may declare a model."""
    offenders: list[str] = []
    for path, tree in app_trees():
        offenders.extend(_positional_field_default_offenders(tree, path))
    assert not offenders, (
        f"{len(offenders)} pydantic fields declare their default in a position "
        f"a checker cannot see, which makes them look required to every "
        f"constructor call and is what pushed this codebase's model construction "
        f"into untyped dicts.\n" + "\n".join(f"  {why}" for why in offenders)
    )


def test_pydantic_default_visibility_detector_bites() -> None:
    """The red half, and the clean half -- a detector that cannot fail is a lie."""
    positional = (
        "from pydantic import BaseModel, Field\n"
        "class M(BaseModel):\n"
        '    etag: str | None = Field(None, description="Origin ETag.")\n'
    )
    keyword = (
        "from pydantic import BaseModel, Field\n"
        "class M(BaseModel):\n"
        '    robots: bool = Field(default=True, description="Honor robots.txt.")\n'
    )
    inside_annotated = (
        "from typing import Annotated\n"
        "from pydantic import BaseModel, Field\n"
        "class M(BaseModel):\n"
        '    context: Annotated[str, Field("", description="Context.")] = ""\n'
    )
    assert_fires_once(
        _positional_field_default_offenders, {positional: "looks required"}, pathlib.Path("bites.py")
    )
    assert_fires_once(
        _positional_field_default_offenders, {keyword: "looks required"}, pathlib.Path("bites.py")
    )
    assert_fires_once(
        _positional_field_default_offenders, {inside_annotated: "can drift"}, pathlib.Path("bites.py")
    )
    assert_stays_clean(
        _positional_field_default_offenders,
        (
            # The form the fix produces: the default sits where a checker reads it, on the assignment, stated
            # once.
            "from typing import Annotated\n"
            "from pydantic import BaseModel, Field\n"
            "class M(BaseModel):\n"
            '    etag: Annotated[str | None, Field(description="Origin ETag.")] = None\n'
            '    context: Annotated[str, Field(description="Context.")] = ""\n'
            "    plain: str | None = None\n",
            # Ellipsis is really required, and a checker is right about it.
            "from pydantic import BaseModel, Field\n"
            "class M(BaseModel):\n"
            '    url: str = Field(..., description="Root URL.")\n',
            # default_factory is resolved by a checker, so it is a different question and must not be swept up
            # in this one.
            "from pydantic import BaseModel, Field\n"
            "class M(BaseModel):\n"
            "    links: list[str] = Field(default_factory=list, description='d')\n",
            "class M:\n    a = 1\n",
        ),
        path=pathlib.Path("bites.py"),
    )
