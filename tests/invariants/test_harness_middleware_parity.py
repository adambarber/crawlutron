"""Harness app middleware parity: every test-built ASGI app rides the service stack. Clause one is the
derived enumeration: any tests/ function -- or same-module function pair -- constructing a FastAPI app and
mounting a crawlutron router is a harness app, built only in the one builder module.
"""

from __future__ import annotations

import ast
import pathlib
from collections.abc import Iterator

from starlette.middleware.base import BaseHTTPMiddleware

from tests.chokepoint_walks import enclosing_function, parent_map, trees_under

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
BUILDER_MODULE = "tests/_app_harnesses.py"
GATE_MODULE = "tests/invariants/test_harness_middleware_parity.py"

#: Pinned absolutely so a middleware dropped from ``install_error_surface``
#: reds here too, not only in a parity comparison against a wounded reference.
EXPECTED_MIDDLEWARE = (
    "throttle_anonymous_surfaces",
    "limit_request_body",
    "reject_hostile_origin",
)


def _callee(node: ast.AST) -> str | None:
    """The bare or attribute name of a Call's callee, or None for a non-Call."""
    if isinstance(node, ast.Call):
        return getattr(node.func, "id", None) or getattr(node.func, "attr", None)
    return None


def _calls(function: ast.AST, callee: str, *, keyword: str | None = None) -> bool:
    """Whether the function calls ``callee`` (bare or attribute) with ``keyword``."""

    def matches(node: ast.Call) -> bool:
        return _callee(node) == callee and (keyword is None or any(kw.arg == keyword for kw in node.keywords))

    return any(isinstance(node, ast.Call) and matches(node) for node in ast.walk(function))


def _test_modules() -> Iterator[tuple[str, ast.Module]]:
    """``(relative, tree)`` for every tests/ module, outside the builder and gate modules."""
    for path, tree in trees_under(REPO_ROOT / "tests"):
        relative = path.relative_to(REPO_ROOT).as_posix()
        if relative in (BUILDER_MODULE, GATE_MODULE):
            continue
        yield relative, tree


def _tests_functions(match) -> list[tuple[str, str]]:
    """Every tests/ function ``match`` accepts, outside the builder and gate modules."""
    found: list[tuple[str, str]] = []
    for relative, tree in _test_modules():
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and match(node):
                found.append((relative, node.name))
    return found


def _fastapi_app_names(tree: ast.Module) -> set[str]:
    """Names bound to a FastAPI() result by direct assignment anywhere in ``tree``."""
    parents = parent_map(tree)
    names: set[str] = set()
    for node in ast.walk(tree):
        if _callee(node) != "FastAPI":
            continue
        owner = parents.get(id(node))
        if isinstance(owner, ast.Assign) and owner.value is node:
            names.update(t.id for t in owner.targets if isinstance(t, ast.Name))
        elif isinstance(owner, ast.AnnAssign) and owner.value is node and isinstance(owner.target, ast.Name):
            names.add(owner.target.id)
    return names


def _mounted_name(node: ast.Call) -> str | None:
    """The Name an include_router call mounts onto, or None when not a simple attribute call."""
    value = getattr(node.func, "value", None)
    return value.id if isinstance(value, ast.Name) else None


def _module_split_builders(relative: str, tree: ast.Module) -> list[tuple[str, str]]:
    """Split builders in one module: a FastAPI() app named ``X`` mounted via ``X.include_router`` in a
    different function. Name-linked only, so an app const plus an unrelated receiver never pairs."""
    app_names = _fastapi_app_names(tree)
    if not app_names:
        return []
    parents = parent_map(tree)
    found: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and _callee(node) == "include_router"):
            continue
        receiver = _mounted_name(node)
        if receiver not in app_names:
            continue
        owner = enclosing_function(node, parents)
        found.append((relative, owner.name if owner is not None else "<module>"))
    return found


def _harness_functions() -> list[tuple[str, str]]:
    """Every tests/ function -- or split module pair -- constructing a FastAPI app and mounting a router."""
    found = list(_tests_functions(lambda node: _calls(node, "FastAPI") and _calls(node, "include_router")))
    for relative, tree in _test_modules():
        found.extend(_module_split_builders(relative, tree))
    return sorted(set(found))


def _stack_opt_outs() -> list[tuple[str, str]]:
    """Functions outside the builder module passing safety_stack=False: the opt-out is the declared way to
    get a stack-less probe, but it is only gate-visible in the builder module's own vocabulary -- an
    out-of-home caller opting out recreates the drift this gate kills.
    """
    return _tests_functions(lambda node: _calls(node, "harness_app", keyword="safety_stack"))


def test_harness_apps_are_built_only_by_the_one_builder():
    findings = _harness_functions()
    assert not findings, (
        "harness apps must be built by tests/_app_harnesses.py, found "
        f"{len(findings)} out-of-home builders: {findings}"
    )


SPLIT_BUILDER_SOURCE = """
def _build_app():
    app = FastAPI()
    return app


def _mount(app):
    app.include_router(some_router)
"""

SINGLE_BUILDER_SOURCE = """
def build():
    app = FastAPI()
    app.include_router(some_router)
    return app
"""

UNLINKED_MOUNT_SOURCE = """
APP = FastAPI()


def unrelated():
    other.include_router(some_router)
"""


def test_split_construction_is_a_harness_builder():
    """FastAPI() in helper A and include_router in helper B of one module is a split builder."""
    tree = ast.parse(SPLIT_BUILDER_SOURCE)
    assert _module_split_builders("tests/_split.py", tree) == [("tests/_split.py", "_mount")]


def test_single_function_builder_stays_classified():
    """Both calls in one function still classify -- the 14th-builder shape stays green."""
    tree = ast.parse(SINGLE_BUILDER_SOURCE)
    assert _module_split_builders("tests/_one.py", tree) == [("tests/_one.py", "build")]


def test_unlinked_router_mount_is_not_a_builder():
    """An app const plus an unrelated include_router receiver is never paired as a split builder."""
    assert _module_split_builders("tests/_none.py", ast.parse(UNLINKED_MOUNT_SOURCE)) == []


def test_safety_stack_opt_out_is_declared_only_in_the_builder_module():
    findings = _stack_opt_outs()
    assert not findings, (
        "safety_stack=False is a builder-module-declared opt-out; out-of-home "
        f"callers bypass the parity gate: {findings}"
    )


def _dispatch_chain(app) -> tuple[str, ...]:
    node = app.build_middleware_stack()
    chain: list[str] = []
    while node is not None:
        if isinstance(node, BaseHTTPMiddleware):
            chain.append(node.dispatch_func.__qualname__.rsplit(".", 1)[-1])
        node = getattr(node, "app", None)
    return tuple(chain)


def test_harness_builder_mounts_the_service_middleware_stack():
    from fastapi import FastAPI

    from crawlutron.api.crawl_router import router as crawl_router
    from tests._app_factory import service_app
    from tests._app_harnesses import harness_app

    reference = _dispatch_chain(service_app())
    assert reference == EXPECTED_MIDDLEWARE, reference
    assert _dispatch_chain(harness_app(crawl_router)) == reference
    assert _dispatch_chain(harness_app(crawl_router, safety_stack=False)) == _dispatch_chain(FastAPI())
