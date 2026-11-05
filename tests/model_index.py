"""Find a declared type by name without a hand-kept table: the ``crawlutron/models`` split
that forbids re-exports makes any dict go stale silently, so the index is DERIVED
by walking the package -- a duplicate declaration raises, and the tree never drifts.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from typing import TypeVar

from crawlutron import models as _models_package

_T = TypeVar("_T")

_INDEX: dict[str, type] | None = None


def _build_index() -> dict[str, type]:
    found: dict[str, type] = {}
    for info in pkgutil.iter_modules(_models_package.__path__):
        module = importlib.import_module(f"{_models_package.__name__}.{info.name}")
        for name, declared in vars(module).items():
            if not inspect.isclass(declared) or declared.__module__ != module.__name__:
                continue
            if name in found:
                raise LookupError(
                    f"{name} is declared in both {found[name].__module__} and {module.__name__}"
                )
            found[name] = declared
    return found


def declared_types() -> dict[str, type]:
    """Every class the ``crawlutron.models`` package declares, keyed by name."""
    global _INDEX
    if _INDEX is None:
        _INDEX = _build_index()
    return _INDEX


def resolve_declared_type(name: str) -> type[_T]:
    """The class ``name`` names in ``crawlutron/models``, whatever file declares it."""
    try:
        resolved: type[_T] = declared_types()[name]  # dict[str, type] narrows to type[_T]
    except KeyError:
        raise LookupError(f"{name} is not declared under crawlutron/models") from None
    return resolved
