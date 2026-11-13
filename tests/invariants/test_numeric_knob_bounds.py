"""Every caller-supplied numeric wire knob carries its bound constraints: a pydantic registry walk over both
wire homes from every ``*Request`` model, following nested/Annotated references transitively, demanding
``ge/gt`` and ``allow_inf_nan=False`` for floats.
"""

import ast
import importlib
import pathlib
import types
import typing
from typing import Final

import pydantic.types
from pydantic import BaseModel

import crawlutron.models

_ENTRY_SUFFIX: Final[str] = "Request"

_APP_MODELS_DIR: Final[pathlib.Path] = pathlib.Path(crawlutron.models.__file__).parent

#: Fields on caller-facing models that are NOT caller-supplied numeric knobs, stated
#: by name so the walk's wording can never silently widen or shrink it.
_OUT_OF_SCOPE_FIELDS: Final[frozenset[str]] = frozenset()

_BOUND_KINDS: Final[frozenset[str]] = frozenset({"ge", "gt", "le", "lt"})


def _entry_models() -> set[type[BaseModel]]:
    """Every ``*Request`` model in either wire home."""
    found: set[type[BaseModel]] = set()
    for home in ("crawlutron.models", "crawlutron_sdk.models"):
        module = importlib.import_module(home)
        package_dir = pathlib.Path(str(module.__file__)).parent if home == "crawlutron.models" else None
        module_names = (
            [home]
            if package_dir is None
            else [f"{home}.{p.stem}" for p in sorted(package_dir.glob("*.py")) if p.stem != "__init__"]
        )
        for name in module_names:
            for attr in vars(importlib.import_module(name)).values():
                if isinstance(attr, type) and issubclass(attr, BaseModel):
                    if attr.__name__.endswith(_ENTRY_SUFFIX):
                        found.add(attr)
    assert found, "the walk resolved no entry models; it would silently guard nothing"
    return found


def _numeric_chain(annotation: object) -> tuple[type, bool] | None:
    """``(base_type, is_float)`` under one strip of ``None`` in an Optional.

    ``int | None`` counts (bounds apply when present); a ``int | float`` union
    is treated as float (the wider constraint governs); anything else is None.
    """
    origin = typing.get_origin(annotation)
    if origin in (typing.Union, types.UnionType):
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        if any(a is float for a in args):
            return (float, True)
        if any(a is int for a in args):
            return (int, False)
        return None
    if annotation is float:
        return (float, True)
    if annotation is int:
        return (int, False)
    return None


def _constraints(field_info: object) -> tuple[bool, bool]:
    """``(has_bound, inf_nan_disabled)`` read from resolved FieldInfo metadata."""
    has_bound = False
    inf_nan_disabled = False
    for meta in getattr(field_info, "metadata", ()):
        kind = type(meta).__name__.lower()
        if kind in _BOUND_KINDS:
            has_bound = True
        if isinstance(meta, pydantic.types.AllowInfNan) and meta.allow_inf_nan is False:
            inf_nan_disabled = True
    return has_bound, inf_nan_disabled


def _field_lineno(cls: type[BaseModel], field_name: str) -> tuple[pathlib.Path, int]:
    """The file:line one field is declared on, for the red message."""
    module = __import__(cls.__module__, fromlist=["*"])
    source_file = pathlib.Path(str(module.__file__))
    tree = ast.parse(source_file.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == cls.__name__:
            for statement in node.body:
                if (
                    isinstance(statement, ast.AnnAssign)
                    and isinstance(statement.target, ast.Name)
                    and statement.target.id == field_name
                ):
                    return source_file, statement.lineno
    return source_file, 0


def _numeric_knob_offenders(extra_entries: list[type[BaseModel]] | None = None) -> list[str]:
    """The registry walk itself: every unbounded caller-facing numeric, named."""
    offenders: list[str] = []
    seen: set[type[BaseModel]] = set()

    def walk(cls: type[BaseModel], depth: int) -> None:
        if cls in seen or depth > 12:
            return
        seen.add(cls)
        for field_name, field_info in cls.model_fields.items():
            got = _numeric_chain(field_info.annotation)
            if got is None:
                nested = field_info.annotation
                if isinstance(nested, type) and issubclass(nested, BaseModel):
                    walk(nested, depth + 1)
                continue
            base, is_float = got
            if isinstance(base, type) and issubclass(base, BaseModel):
                walk(base, depth + 1)
                continue
            if field_name in _OUT_OF_SCOPE_FIELDS:
                continue
            has_bound, inf_nan_disabled = _constraints(field_info)
            source_file, line = _field_lineno(cls, field_name)
            # cwd and __file__ can differ in spelling on a firmlink checkout; a label
            # must never crash the walk, so fall back to the exact absolute path.
            try:
                label_rel = source_file.relative_to(pathlib.Path.cwd())
            except ValueError:
                label_rel = source_file
            label = f"{label_rel}:{line} {cls.__name__}.{field_name}"
            if is_float and not (has_bound and inf_nan_disabled):
                missing = [] if has_bound else ["a floor bound"]
                if not inf_nan_disabled:
                    missing.append("allow_inf_nan=False")
                offenders.append(f"{label}: float knob lacks {' and '.join(missing)}")
            elif not is_float and not has_bound:
                offenders.append(f"{label}: int knob carries no floor bound")

    entries = sorted(set(_entry_models()) | set(extra_entries or []), key=lambda m: m.__name__)
    for entry in entries:
        walk(entry, 0)
    return offenders


def test_every_caller_facing_numeric_knob_is_bounded():
    """The live tree satisfies the invariant: the walk returns zero offenders.

    Guards itself from passing on nothing: if the walk resolves no entry
    models or no numeric fields at all, the premise asserts before the claim.
    """
    assert _entry_models(), "no *Request entry models resolved; the walk guards nothing"
    offenders = _numeric_knob_offenders()
    assert not offenders, (
        "every caller-supplied numeric wire knob needs a floor (ge/gt) and, for floats, "
        "allow_inf_nan=False at the model edge -- the documented-sentinel escape is a "
        "named constant plus comment, never a silent literal:\n  " + "\n  ".join(offenders)
    )


class _UnboundedFiniteFloat(BaseModel):
    """Mutation probe (a): finite float, no floor."""

    ratio: float


class _UnboundedOptionalInt(BaseModel):
    """Mutation probe (b): int | None, no floor."""

    retries: int | None = None


class _NanAdmittableOptionalFloat(BaseModel):
    """Mutation probe (c): float | None, no allow_inf_nan=False."""

    ratio: float | None = None


def test_an_unbounded_numeric_field_reds_naming_it():
    """The bite, in memory: each unbounded shape reds, naming its defect."""
    offenders = _numeric_knob_offenders(
        [_UnboundedFiniteFloat, _UnboundedOptionalInt, _NanAdmittableOptionalFloat]
    )
    floor_msg = "_UnboundedFiniteFloat.ratio: float knob lacks a floor bound and allow_inf_nan=False"
    int_msg = "_UnboundedOptionalInt.retries: int knob carries no floor bound"
    nan_msg = "_NanAdmittableOptionalFloat.ratio: float knob lacks a floor bound and allow_inf_nan=False"
    assert any(floor_msg in o for o in offenders)
    assert any(int_msg in o for o in offenders)
    assert any(nan_msg in o for o in offenders)
    assert not any("_UnboundedOptionalInt.retries" in o and "allow_inf_nan" in o for o in offenders)


def test_the_union_walk_sees_pep_604_unions():
    """int | None (types.UnionType on 3.14) is recognized, not just typing.Union."""
    got = _numeric_chain(_UnboundedOptionalInt.model_fields["retries"].annotation)
    assert got == (int, False), got
    got = _numeric_chain(_NanAdmittableOptionalFloat.model_fields["ratio"].annotation)
    assert got == (float, True), got
