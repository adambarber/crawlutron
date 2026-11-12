"""Harness app middleware parity: every test-built ASGI app rides the service stack. Clause one is the
derived enumeration: any tests/ function constructing a FastAPI app and mounting a crawlutron router is a
harness app, built only in the one builder module.
"""

from __future__ import annotations

import ast
import pathlib

from starlette.middleware.base import BaseHTTPMiddleware

from tests.chokepoint_walks import trees_under

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


def _calls(function: ast.AST, callee: str, *, keyword: str | None = None) -> bool:
    """Whether the function calls ``callee`` (bare or attribute) with ``keyword``."""

    def matches(node: ast.Call) -> bool:
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        return name == callee and (keyword is None or any(kw.arg == keyword for kw in node.keywords))

    return any(isinstance(node, ast.Call) and matches(node) for node in ast.walk(function))


def _tests_functions(match) -> list[tuple[str, str]]:
    """Every tests/ function ``match`` accepts, outside the builder and gate modules."""
    found: list[tuple[str, str]] = []
    for path, tree in trees_under(REPO_ROOT / "tests"):
        relative = path.relative_to(REPO_ROOT).as_posix()
        if relative in (BUILDER_MODULE, GATE_MODULE):
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and match(node):
                found.append((relative, node.name))
    return found


def _harness_functions() -> list[tuple[str, str]]:
    """Every tests/ function constructing a FastAPI app and mounting a router."""
    return _tests_functions(lambda node: _calls(node, "FastAPI") and _calls(node, "include_router"))


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
