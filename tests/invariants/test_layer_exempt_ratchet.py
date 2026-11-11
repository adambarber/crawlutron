"""The layer-exempt ratchet (#664): opting out is an explicit, gate-asserted act.

The pinned row list moves DOWN only; adding a row is RED until the owner
accepts the same-commit justification.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest
from fastapi.routing import APIRoute

import crawlutron.api.route_layer as _route_layer_module
from crawlutron.api.route_layer import ANONYMOUS_METERED, LAYER_EXEMPT_RATCHET, ExemptRow
from crawlutron.api.router_registry import ROUTER_MODULES
from tests._app_factory import fresh_app
from tests._async_harnesses import serve
from tests.invariants.test_route_layer import _offender_module


def _guarded_keys() -> set[tuple[str, str]]:
    """The (method, path) pairs the layer guard protects, read off the routers."""
    from crawlutron.api.route_layer import LAYER_GUARDS

    keys: set[tuple[str, str]] = set()
    for router in ROUTER_MODULES:
        for route in router.routes:
            if not isinstance(route, APIRoute):
                continue
            names = {dep.call.__name__ for dep in route.dependant.dependencies if dep.call is not None}
            if names & LAYER_GUARDS:
                for method in route.methods or ():
                    keys.add((method.lower(), route.path))
    return keys


def _served_routes(*, include_foreign: bool = False) -> set[tuple[str, str]]:
    """Every (method, path) the real app serves, factory routers included.

    ``include_foreign`` keeps framework-owned routes (FastAPI's docs closures
    live in site-packages) for EXISTENCE checks -- a declared metered surface
    must exist as a route even when its code is not ours. The package filter
    stays on for ownership tests (the converse, offender naming).
    """
    keys: set[tuple[str, str]] = set()

    def collect(route: object) -> None:
        methods = getattr(route, "methods", None) or ()
        path = getattr(route, "path", None)
        if path and methods:
            keys.update((method.lower(), path) for method in methods)

    _walk_served(collect, include_foreign=include_foreign)
    return keys


def _walk_served(visit: Callable[[object], None], *, include_foreign: bool = False) -> None:
    """Depth-first over the real app's production routes.

    Test-added routes (a door probe mounting /boom on the shared app) are
    pollution, not service surface: only endpoints whose code object lives
    inside the crawlutron PACKAGE count -- a mutant planted there is still
    caught. The boundary is a Path comparison against the package root, not a
    checkout-path substring: a checkout not named crawlutron still greens
    honestly (review F3 artifact, mutation-proven). ``include_foreign`` adds
    framework-owned routes for existence checks only.
    """
    import crawlutron

    package_root = Path(crawlutron.__file__).resolve().parent
    from tests._app_factory import service_app

    app = service_app()
    assert app is not None

    def walk(routes: Sequence[object]) -> None:
        for route in routes:
            if type(route).__name__ == "_IncludedRouter":
                inner = getattr(route, "original_router", None)
                walk(getattr(inner, "routes", ()))
                continue
            endpoint = getattr(route, "endpoint", None)
            code = getattr(endpoint, "__code__", None)
            if code is None:
                continue
            ours = package_root in Path(code.co_filename).resolve().parents
            if ours or include_foreign:
                visit(route)

    walk(app.router.routes)


def _declare_exempt_row_and_boot(monkeypatch: pytest.MonkeyPatch, row: ExemptRow) -> RuntimeError:
    """Boot with `row` added to the ratchet; return the RuntimeError the guard raises."""
    monkeypatch.setattr(
        _route_layer_module,
        "LAYER_EXEMPT_RATCHET",
        LAYER_EXEMPT_RATCHET + (row,),
    )
    with pytest.raises(RuntimeError) as exc_info:
        fresh_app()
    return exc_info.value


def test_exempt_rows_match_real_routes():
    """A stale exemption row (renamed or deleted route) fails the gate."""
    routes = _guarded_keys() | _served_routes()
    for row in LAYER_EXEMPT_RATCHET:
        path = row.path.split(":")[0]
        key = (row.method, path)
        assert key in routes, f"exempt row {key} no longer matches a served route"


def test_exempt_ratchet_never_increases():
    """The pinned literal only moves down, in the commit that earns it."""
    assert len(LAYER_EXEMPT_RATCHET) == 6, (
        "LAYER_EXEMPT_RATCHET is pinned at 6; adding a row demands the owner "
        "accept the row's justification in the same commit and lower/raise this "
        "literal deliberately -- an uncommented bump is the evasion this test exists for"
    )


def test_every_served_route_is_guarded_or_exempt():
    """The converse: nothing the app serves escapes the layer silently.

    served - guarded - exempt must be empty. The #664 PENDING table was sealed
    at zero (#668): every previously-pending surface meters through the
    anonymous bucket (api/anonymous_throttle.py), so the table and its pins
    are gone -- this unconditional assertion is the converse guard.
    """
    guarded = _guarded_keys()
    exempt = {(row.method, row.path.split(":")[0]) for row in LAYER_EXEMPT_RATCHET}
    anonymous = {(row.method, row.path) for row in ANONYMOUS_METERED}
    # Starlette derives HEAD from every GET route, so HEAD mirrors GET here.
    raw = _served_routes()
    served = raw - {("head", path) for _, path in raw} | {
        ("get", path) for method, path in raw if method == "head"
    }
    ungoverned = []
    for method, path in sorted(served - guarded - exempt - anonymous):
        for route in _routes_with_key((method, path)):
            ungoverned.append(f"{route}:{method.upper()} {path}")
    assert not ungoverned, (
        "served routes outside the auth+metering layer and the exemption ratchet "
        "(land its metering; the PENDING table is sealed shut at zero -- a new "
        "unmetered surface needs an owner-reviewed row): " + ", ".join(ungoverned)
    )


def test_anonymous_rows_match_real_routes():
    """A stale anonymous-metered row (renamed or deleted route) fails the gate."""
    routes = _served_routes(include_foreign=True)
    for row in ANONYMOUS_METERED:
        key = (row.method, row.path)
        assert key in routes, f"anonymous-metered row {key} no longer matches a served route"


def test_anonymous_ratchet_never_increases():
    """The pinned literal moves only in the commit that earns it (#668 fix-up:
    4 -> 10 is the review's completeness LANDING of the enumerated exempt GETs
    (six: health, ready, well-known, capabilities, agent-hints, openapi.json --
    the directive's "five" count was off by one against its own list) -- a
    scope-completion move, authorized in the commit body, not a budget raise)."""
    assert len(ANONYMOUS_METERED) == 10, (
        "ANONYMOUS_METERED is pinned at 10; adding a row demands the metering "
        "landed in the same commit and this literal lowered/raised deliberately"
    )


def test_anonymous_rows_carry_a_justification():
    """A metered-surface declaration without a stated reason is not an explicit act."""
    for row in ANONYMOUS_METERED:
        assert isinstance(row, ExemptRow) and len(row.reason) > 20, f"row {row} lacks a real justification"


def _routes_with_key(key: tuple[str, str]) -> list[str]:
    """The source file+line of every served route definition matching key."""
    hits: list[str] = []

    def collect(route: object) -> None:
        if not isinstance(route, APIRoute):
            path, methods = getattr(route, "path", None), getattr(route, "methods", None)
            if path == key[1] and methods and key[0] in {m.lower() for m in methods}:
                hits.append(repr(getattr(route, "endpoint", None)))
            return
        for method in route.methods or ():
            if (method.lower(), route.path) == key:
                hits.append(_offender_module(route))

    _walk_served(collect)
    return [h for h in hits if h and h != "None"]


def test_every_row_carries_a_justification():
    """An exemption without a stated reason is not an explicit act."""
    for row in LAYER_EXEMPT_RATCHET:
        assert isinstance(row, ExemptRow) and len(row.reason) > 20, f"row {row} lacks a real justification"


async def test_boot_refuses_a_declared_exempt_route_without_a_mounted_door(monkeypatch):
    """Fail closed: a declared exemption row with no mounted route keeps the app from booting."""
    bogus = ExemptRow(
        "get",
        "/v1/no-such-exempt-route",
        "a route nothing mounts, used to prove the boot guard",
    )
    exc = _declare_exempt_row_and_boot(monkeypatch, bogus)
    assert "GET /v1/no-such-exempt-route" in str(exc)
    assert "no mounted route" in str(exc)


async def test_boot_refuses_exempt_row_behind_credential_refusing_guard(monkeypatch):
    """A row on a guard_admin_request-mounted path refuses boot naming the row and the refusing arm."""
    row = ExemptRow("get", "/v1/keys", "pinned by the guarded arm")
    exc = _declare_exempt_row_and_boot(monkeypatch, row)
    text = str(exc)
    assert "GET /v1/keys" in text
    assert "credential-refusing guard" in text


async def test_boot_detects_renamed_guard_by_function_identity(monkeypatch):
    """A renamed guard (wrong __name__) evades name-match but is caught by identity comparison."""
    monkeypatch.setattr(_route_layer_module.guard_admin_request, "__name__", "renamed_admin_guard")
    row = ExemptRow("get", "/v1/keys", "pinned by identity comparison")
    exc = _declare_exempt_row_and_boot(monkeypatch, row)
    assert re.search(r"GET /v1/keys.*credential-refusing guard", str(exc))


async def test_declared_exempt_routes_are_credential_free():
    """Every declared exemption row answers without a caller credential."""
    async with serve(fresh_app()) as client:
        for row in LAYER_EXEMPT_RATCHET:
            path = row.path.split(":")[0]
            if row.method == "post":
                response = await client.post(path, json={"url": "https://example.com/"})
            else:
                response = await client.get(path)
            assert response.status_code == 200, f"{row.method.upper()} {path}: {response.text}"


async def test_non_exempt_served_route_refuses_anonymous_callers():
    """Converse boundary: a served route outside the ratchet refuses anonymous callers."""
    async with serve(fresh_app()) as client:
        response = await client.post("/v1/keys")
    assert response.status_code == 401, response.text
