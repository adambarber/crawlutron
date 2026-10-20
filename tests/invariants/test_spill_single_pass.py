"""Structural rules for the spill loader and pipeline wiring: ``ResultSpill.load_ordered``
rebuilds in one pass and the browser-pool size derives at the construction site --
contracts no behavior test can observe.
"""

from __future__ import annotations

import ast

from tests.chokepoint_walks import app_trees, offender_at, relative_posix


def test_spill_load_ordered_rebuilds_in_one_pass():
    for path, tree in app_trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "load_ordered" and "spill" in str(path):
                # A Protocol ``...`` stub declares the shape, it does not implement it.
                if (
                    len(node.body) == 1
                    and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)
                ):
                    continue
                slots_seen = 0
                for child in ast.walk(node):
                    if (
                        isinstance(child, ast.Assign)
                        and isinstance(child.targets[0], ast.Subscript)
                        and isinstance(child.targets[0].value, ast.Name)
                        and child.targets[0].value.id.endswith("slots")
                    ):
                        slots_seen += 1
                    if (
                        isinstance(child, ast.Call)
                        and isinstance(child.func, ast.Attribute)
                        and child.func.attr == "sort"
                    ):
                        raise AssertionError(offender_at(path, child, "spill load_ordered must not sort"))
                if slots_seen == 0:
                    raise AssertionError(
                        offender_at(path, node, "spill load_ordered must index results into slots")
                    )


def test_browser_pool_size_is_derived_at_the_pipeline_construction_site():
    """The pool size env flow must reach where pools are built, not just helpers.

    Site wiring is not observable output: an uncalled helper passes every
    behavior test while the fleet over-subscribes.
    """
    for path, tree in app_trees():
        if relative_posix(path).endswith("request_pipeline.py"):
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "derive_browser_pool_size"
                ):
                    return
            raise AssertionError(
                offender_at(path, tree.body[0], "request_pipeline never calls derive_browser_pool_size")
            )
