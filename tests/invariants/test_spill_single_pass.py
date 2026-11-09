"""Structural rules for the spill loader and pipeline wiring: the spill store's
``load_ordered`` reassembles in one pass (the ORDER BY lives in the SQL, never a
client-side sort) and the browser-pool size derives at the construction site --
contracts no behavior test can observe.
"""

from __future__ import annotations

import ast

from tests.chokepoint_walks import app_trees, offender_at, relative_posix


def test_spill_load_ordered_reassembles_without_a_client_side_sort():
    for path, tree in app_trees():
        if not relative_posix(path).endswith("storage/result_store.py"):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "load_ordered":
                for child in ast.walk(node):
                    is_attr_sort = (
                        isinstance(child, ast.Call)
                        and isinstance(child.func, ast.Attribute)
                        and child.func.attr == "sort"
                    )
                    is_name_sort = (
                        isinstance(child, ast.Call)
                        and isinstance(child.func, ast.Name)
                        and (child.func.id == "sorted")
                    )
                    if is_attr_sort or is_name_sort:
                        raise AssertionError(
                            offender_at(
                                path,
                                child,
                                "spill reassembly orders via the SELECT, never a client-side sort",
                            )
                        )
                return
        raise AssertionError(offender_at(path, tree.body[0], "result_store lost its load_ordered"))


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
