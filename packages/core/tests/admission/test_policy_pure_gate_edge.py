"""Pure-gate edge invariant for ``crawlutron.admission.policy``.

``read_policy`` is the one read edge: total, side-effect free, and importing
nothing but data modelling. This file pins the seam statically and at runtime.
"""

from __future__ import annotations

import ast
from collections.abc import Callable

from crawlutron.admission.policy import (
    PolicyLike,
    read_policy,
    stored_policy,
    validate_wire_policy,
)
from crawlutron.models.json_value import JsonValue
from tests.chokepoint_paths import REPO_ROOT
from tests.chokepoint_walks import discovered_files

#: Imports the policy module must never make: the effect-container seam plus
#: any network, database, or side-effect stdlib/third-party module.
_FORBIDDEN_IMPORTS: tuple[str, ...] = (
    "service.effects",
    "requests",
    "httpx",
    "aiohttp",
    "sqlite3",
    "psycopg",
    "psycopg2",
)

_POLICY_INPUTS: tuple[PolicyLike | None, ...] = (
    None,
    {},
    {"limits": {"max_pages": 7}},
    {"limits": {"max_pages": 7, "max_retries": 2}},
)

_WIRE_INPUTS: tuple[PolicyLike | None, ...] = (
    {},
    {"limits": {"max_pages": 7}},
    {"limits": {"max_pages": 7, "max_retries": 2}},
)

# Read the policy module source via the sanctioned discovered_files door.
# The module lives at packages/core/src/crawlutron/admission/policy.py
_POLICY_SOURCE = discovered_files(
    REPO_ROOT / "packages/core/src/crawlutron/admission", "policy.py", include_content=True
)[REPO_ROOT / "packages/core/src/crawlutron/admission/policy.py"]


def _imported_references(source: str) -> list[tuple[int, str]]:
    """Every ``(line, dotted reference)`` an import statement in ``source`` names."""
    references: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                references.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            for alias in node.names:
                if alias.name == "*":
                    references.append((node.lineno, node.module))
                else:
                    references.append((node.lineno, f"{node.module}.{alias.name}"))
    return references


def _offending_imports(source: str, forbidden: tuple[str, ...]) -> list[str]:
    """``file:line imports reference`` lines for every forbidden reference."""
    offenders: list[str] = []
    for line, reference in _imported_references(source):
        dotted = f".{reference}."
        for bad in forbidden:
            if f".{bad}." in dotted:
                offenders.append(
                    f"packages/core/src/crawlutron/admission/policy.py:{line} imports {reference}"
                )
    return offenders


def test_policy_module_imports_no_service_effects():
    """The effect-container seam is not imported by the policy module."""
    offenders = _offending_imports(_POLICY_SOURCE, ("service.effects",))
    assert not offenders, "\n".join(offenders)


def test_policy_module_imports_no_network_or_db_modules():
    """No requests/httpx/aiohttp/sqlite3/psycopg import reaches the policy module."""
    offenders = _offending_imports(_POLICY_SOURCE, _FORBIDDEN_IMPORTS[1:])
    assert not offenders, "\n".join(offenders)


def _call_all(fn: Callable[[PolicyLike | None], object]) -> list[object]:
    """Run ``fn`` once per pinned input, returning outputs in order."""
    return [fn(raw) for raw in _POLICY_INPUTS]


def test_read_policy_is_pure():
    """Same input, same policy; the caller's mapping is never touched."""
    assert _call_all(read_policy) == _call_all(read_policy)
    raw: dict[str, JsonValue] = {"limits": {"max_pages": 7}}
    before = dict(raw)
    read_policy(raw)
    assert raw == before


def test_stored_policy_is_pure():
    """Same input, same stored dump; the caller's mapping is never touched."""
    assert _call_all(stored_policy) == _call_all(stored_policy)
    raw: dict[str, JsonValue] = {"limits": {"max_pages": 7}}
    before = dict(raw)
    stored_policy(raw)
    assert raw == before


def test_validate_wire_policy_is_pure():
    """Same input, same wire verdict; the caller's mapping is never touched."""
    assert [validate_wire_policy(raw) for raw in _WIRE_INPUTS] == [
        validate_wire_policy(raw) for raw in _WIRE_INPUTS
    ]
    raw: dict[str, JsonValue] = {"limits": {"max_pages": 7}}
    before = dict(raw)
    validate_wire_policy(raw)
    assert raw == before
