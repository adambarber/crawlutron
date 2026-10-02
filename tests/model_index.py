"""Find a declared type by name, without a hand-kept name-to-module table.

Every declaration lives in its own file under ``app/models``, named for it, and
nothing re-exports anything (``app/models/__init__.py`` says why). The price of
that rule is that ``getattr(app.models, "EngineCrawlResult")`` no longer works,
and the obvious repair -- a dict of name to module -- is exactly the hand-
maintained mirror the split exists to remove: it goes stale silently the day
somebody adds a declaration, and a stale entry is a ``LookupError`` that reads
like a typo rather than like the drift it is.

So the index is DERIVED. Walk the package, collect the classes each module
declares, and the answer to "where does this name live" is a property of the
tree rather than a fact written down next to it. Adding a file needs no edit
here; renaming a file cannot desynchronise this.

The package rule makes the ambiguity small: one concept per file, so a name
appears in one module. Should two modules ever declare the same name that is a
duplicate declaration, and raising on it here makes the collision loud at first
use instead of a coin flip about which of the two wins.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from typing import TypeVar

from app import models as _models_package

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
    """Every class the ``app.models`` package declares, keyed by name."""
    global _INDEX
    if _INDEX is None:
        _INDEX = _build_index()
    return _INDEX


def resolve_declared_type(name: str) -> type[_T]:
    """The class ``name`` names in ``app/models``, whatever file declares it."""
    try:
        return declared_types()[name]  # type: ignore[return-value]
    except KeyError:
        raise LookupError(f"{name} is not declared under app/models") from None
