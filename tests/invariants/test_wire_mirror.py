"""Wire models mirror every domain field; advertised keys are declared."""

from __future__ import annotations

import ast
import pathlib
from enum import StrEnum
from typing import get_args

import pytest

from tests.chokepoint_walks import REPO_ROOT, file_tree

# --- A wire model may not silently drop a domain model's field --------------- app/models/taxonomy.py holds
# the internal dataclasses and the pydantic response models are the ones under app/models/
_WIRE_MODEL_DIR = "app/models"


def _stem_pairs(package: str) -> dict[str, tuple[pathlib.Path, ast.ClassDef, ast.ClassDef]]:
    """``stem -> (owner module, owner record, projection)`` for every pair that exists.

    The owner is ``<stem>.py`` and the projection ``<stem>_response.py``, so the
    pairing is derived from file names the one-home rule forces to be findable.
    """
    root = REPO_ROOT / package
    pairs: dict[str, tuple[pathlib.Path, ast.ClassDef, ast.ClassDef]] = {}
    for owner_path in sorted(root.glob("*.py")):
        if owner_path.name == "__init__.py":
            continue
        twin = root / f"{owner_path.stem}_response.py"
        if not twin.exists():
            continue
        owners = [
            n
            for n in ast.parse(owner_path.read_text(encoding="utf-8")).body
            if isinstance(n, ast.ClassDef) and _is_record(n) and not _is_pydantic_model(n)
        ]
        twins = [
            n
            for n in ast.parse(twin.read_text(encoding="utf-8")).body
            if isinstance(n, ast.ClassDef) and _is_pydantic_model(n)
        ]
        if len(owners) == 1 and len(twins) == 1:
            pairs[owner_path.stem] = (owner_path, owners[0], twins[0])
    return pairs


def _is_dataclass(node: ast.ClassDef) -> bool:
    """True for a ``@dataclass`` class, bare or parameterised."""
    return any(
        (isinstance(d, ast.Name) and d.id == "dataclass")
        or (isinstance(d, ast.Call) and getattr(d.func, "id", None) == "dataclass")
        for d in node.decorator_list
    )


def _is_record(node: ast.ClassDef) -> bool:
    """True for a class that DECLARES its key set: a dataclass or a TypedDict.

    Both spell a closed set of named fields; the TypedDict half is what let an
    extractor's ``dict[str, Any]`` output be checked at all.
    """
    return _is_dataclass(node) or any(
        isinstance(base, ast.Name)
        and base.id in ("TypedDict", "NotRequired")
        or isinstance(base, ast.Subscript)
        and isinstance(base.value, ast.Name)
        and base.value.id in ("TypedDict", "Required", "NotRequired")
        for base in node.bases
    )


def _is_pydantic_model(node: ast.ClassDef) -> bool:
    """True for a class deriving from ``BaseModel``, directly or subscripted."""
    return any(
        isinstance(target := (base.value if isinstance(base, ast.Subscript) else base), ast.Name)
        and target.id == "BaseModel"
        for base in node.bases
    )


def _declared_fields(node: ast.ClassDef) -> list[str]:
    return [
        stmt.target.id
        for stmt in node.body
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
    ]


def _wire_tree() -> ast.Module:
    """Every response model in the package, merged into one comparable tree."""
    merged = ast.Module(body=[], type_ignores=[])
    for path in sorted(REPO_ROOT.glob(f"{_WIRE_MODEL_DIR}/*.py")):
        merged.body.extend(ast.parse(path.read_text(encoding="utf-8")).body)
    return merged


def _wire_mirror_drift(
    pairs: dict[str, tuple[pathlib.Path, ast.ClassDef, ast.ClassDef]],
) -> list[str]:
    """Field names that differ between each owner record and its wire projection."""
    drifted: list[str] = []
    for _stem, (_path, owner, projection) in sorted(pairs.items()):
        name = owner.name
        internal = set(_declared_fields(owner))
        external = set(_declared_fields(projection))
        drifted.extend(
            f"{name}.{field_name} exists internally and is dropped on the wire"
            for field_name in sorted(internal - external)
        )
        drifted.extend(
            f"{name}.{field_name} is declared on the wire and nowhere internally"
            for field_name in sorted(external - internal)
        )
    return drifted


def _synthetic_pair(
    owner_src: str, projection_src: str
) -> dict[str, tuple[pathlib.Path, ast.ClassDef, ast.ClassDef]]:
    """A one-entry pair built from source text, for proofs that plant both halves."""

    def one(src: str) -> ast.ClassDef:
        return next(n for n in ast.parse(src).body if isinstance(n, ast.ClassDef))

    return {"synthetic": (pathlib.Path("synthetic.py"), one(owner_src), one(projection_src))}


def test_wire_models_mirror_every_domain_field():
    """A field cannot disappear between an owner record and its wire projection.

    A filled-but-undeclared field stops existing at the splat; a declared-but-
    unfilled one is ``None`` forever. Pairs come from module stems, not names.
    """
    pairs = _stem_pairs(_WIRE_MODEL_DIR)
    assert pairs, (
        f"No record under {_WIRE_MODEL_DIR}/ has a {chr(95)}response.py projection, so this rule "
        f"compares nothing and passes. That is the failure it exists to catch: pairs are found by "
        f"file name, so a rename that breaks the naming silently empties the rule rather than "
        f"failing it."
    )
    drifted = _wire_mirror_drift(pairs)
    assert not drifted, (
        "A pydantic projection DROPS every key it does not declare, so a field added to an owner "
        "record without a matching field on its projection does not fail loudly -- it quietly "
        "stops existing on the wire. Keep the pair in step: " + "; ".join(drifted)
    )


def test_wire_mirror_detector_bites():
    """A dropped field and a wire-only field both fire; an exact pair stays clean."""
    owner = "@dataclass(frozen=True)\nclass Link:\n    url: str\n    host: str\n    depth: int\n"
    assert not _wire_mirror_drift(
        _synthetic_pair(
            owner,
            "from pydantic import BaseModel\nclass LinkResponse(BaseModel):\n"
            "    url: str\n    host: str\n    depth: int\n",
        )
    )

    drifted = _wire_mirror_drift(
        _synthetic_pair(owner, "class LinkResponse(BaseModel):\n    url: str\n    host: str\n")
    )
    assert len(drifted) == 1, drifted
    assert "Link.depth" in drifted[0] and "dropped on the wire" in drifted[0], drifted

    drifted = _wire_mirror_drift(
        _synthetic_pair(
            owner,
            "class LinkResponse(BaseModel):\n    url: str\n    host: str\n    depth: int\n    score: float\n",
        )
    )
    assert len(drifted) == 1, drifted
    assert "Link.score" in drifted[0] and "nowhere internally" in drifted[0], drifted

    # A bare @dataclass, a subscripted BaseModel[str] base and a total=False TypedDict are three spellings of
    # "declares a key set"; all must be matched.
    assert _wire_mirror_drift(
        _synthetic_pair(
            "@dataclass\nclass Link:\n    url: str\n    host: str\n    depth: int\n",
            "class LinkResponse(BaseModel[str]):\n    url: str\n    host: str\n",
        )
    )
    assert not _wire_mirror_drift(
        _synthetic_pair(
            "class Link(TypedDict, total=False):\n    title: str\n    favicon: str\n",
            "class LinkResponse(BaseModel):\n    title: str\n    favicon: str\n",
        )
    )
    drifted = _wire_mirror_drift(
        _synthetic_pair(
            "class Link(TypedDict):\n    title: str\n",
            "class LinkResponse(BaseModel):\n    title: str\n    favicon: str\n",
        )
    )
    assert drifted == ["Link.favicon is declared on the wire and nowhere internally"], drifted


# --- An advertised payload key may not be dropped by its response model ------- The two discovery endpoints
# build a dict by hand and hand it to FastAPI with a strict `response_model=`.

_ADVERTISED_PAYLOADS = (
    ("app/discovery/capabilities.py", "capabilities_payload", "CapabilitiesResponse"),
    ("app/discovery/agent_hints.py", "agent_hints_payload", "AgentHintsResponse"),
)


def _top_level_function(tree: ast.Module, function_name: str) -> ast.FunctionDef:
    """The module-level ``def`` named ``function_name``, asserted to exist."""
    function = next(
        (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == function_name),
        None,
    )
    assert function is not None, f"{function_name} not found; update the caller"
    return function


def _advertised_keys(tree: ast.Module, function_name: str) -> set[str]:
    """Top-level string keys of a payload function's single returned dict.

    Asserts rather than degrading: reporting "nothing dropped" for a shape this
    gate cannot see would recreate the defect it exists to catch.
    """
    function = _top_level_function(tree, function_name)
    returns = [n for n in ast.walk(function) if isinstance(n, ast.Return)]
    assert len(returns) == 1, (
        f"{function_name} must be a single `return {{...}}` so its advertised keys can be "
        f"read; found {len(returns)} return statements"
    )
    payload = returns[0].value
    assert isinstance(payload, ast.Dict), (
        f"{function_name} must return a dict literal so its keys can be compared against the "
        f"response model; got {type(payload).__name__}"
    )
    return {k.value for k in payload.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}


def _undeclared_advertised_keys(
    payload_tree: ast.Module, wire_tree: ast.Module, function_name: str, model_name: str
) -> list[str]:
    """Advertised payload keys the response model does not declare."""
    model = next((n for n in wire_tree.body if isinstance(n, ast.ClassDef) and n.name == model_name), None)
    assert model is not None, f"{model_name} not found under {_WIRE_MODEL_DIR}/"
    declared = set(_declared_fields(model))
    return [
        f"advertised key {key!r} is dropped by {model_name}"
        for key in sorted(_advertised_keys(payload_tree, function_name) - declared)
    ]


@pytest.mark.parametrize(("payload_module", "function_name", "model_name"), _ADVERTISED_PAYLOADS)
def test_every_advertised_payload_key_is_declared_on_its_response_model(
    payload_module: str, function_name: str, model_name: str
):
    """An advertised key that the response model does not declare is dropped on the wire."""
    _, payload_tree = file_tree(payload_module)
    dropped = _undeclared_advertised_keys(payload_tree, _wire_tree(), function_name, model_name)
    assert not dropped, (
        f"{function_name} advertises keys that {model_name} does not declare, and "
        f"response_model={model_name} silently filters every undeclared key -- so they reach "
        f"the dict-returning tests and never the wire. Add the field to {model_name}: " + "; ".join(dropped)
    )


def test_advertised_payload_detector_bites():
    """An undeclared advertised key fires; an exact pair stays clean; a blind spot raises."""
    wire = ast.parse("class Capabilities(BaseModel):\n    service: str\n    crawl_timing: Timing\n")
    exact = ast.parse("def payload():\n    return {'service': 'x', 'crawl_timing': {}}\n")
    assert not _undeclared_advertised_keys(exact, wire, "payload", "Capabilities")

    drifted = ast.parse(
        "def payload():\n    return {'service': 'x', 'crawl_timing': {}, 'limits': {}, 'extra': 1}\n"
    )
    reported = _undeclared_advertised_keys(drifted, wire, "payload", "Capabilities")
    assert len(reported) == 2, reported
    assert "'extra'" in reported[0] and "dropped by Capabilities" in reported[0], reported
    assert "'limits'" in reported[1], reported

    # A declared field the payload never sends is not drift in this direction: the model is the stricter of
    # the two and a required field is caught by FastAPI.
    assert not _undeclared_advertised_keys(
        ast.parse("def payload():\n    return {'service': 'x'}\n"), wire, "payload", "Capabilities"
    )

    # A payload assembled statement by statement defeats static extraction, and going quiet on that shape
    # would be the very defect this gate exists to stop.
    assembled = ast.parse("def payload():\n    d = {}\n    d['service'] = 'x'\n    return d\n")
    with pytest.raises(AssertionError, match="dict literal"):
        _undeclared_advertised_keys(assembled, wire, "payload", "Capabilities")


# --------------------------------------------------------------------------- One declaration of what a result
# is ---------------------------------------------------------------------------


def _taxonomy_members(enum: type[StrEnum]) -> set[str]:
    return {member.value for member in enum}


def literal_members(alias: object) -> set[str]:
    return set(get_args(alias))


def _constructor_keywords(tree: ast.Module, function_name: str, callee: str) -> set[str]:
    """The keyword field names of the ``callee(...)`` call inside ``function_name``.
    Two internally-consistent spellings of one record are compared field- by-field;
    the pydantic wire model sees neither.
    """
    function = _top_level_function(tree, function_name)
    call = next(
        (n for n in ast.walk(function) if isinstance(n, ast.Call) and getattr(n.func, "id", None) == callee),
        None,
    )
    assert call is not None, f"{callee}(...) not constructed in {function_name}"
    return {kw.arg for kw in call.keywords if kw.arg is not None}


# The two terminal funnels re-spell the shared attempt shape (memory #4729);
# they now live in two modules -- terminal_attempt below transport, the engine funnel here.
_FUNNEL_ATTEMPT_FIELDS = (
    "app/models/result_factory.py:terminal_attempt",
    "app/orchestration/results.py:terminal_engine_result",
)


def _attempt_funnel_field_sets() -> dict[str, set[str]]:
    sets: dict[str, set[str]] = {}
    for spec in _FUNNEL_ATTEMPT_FIELDS:
        module_path, function_name = spec.split(":")
        tree = ast.parse((REPO_ROOT / module_path).read_text(encoding="utf-8"))
        sets[spec] = _constructor_keywords(tree, function_name, "CrawlStrategyAttempt")
    return sets


def test_the_two_terminal_funnels_construct_the_attempt_with_the_same_fields():
    """A field added to one terminal funnel and not the other is red: the
    constructor field list is the shape the two funnels must agree on.
    """
    fields = _attempt_funnel_field_sets()
    reference, other = _FUNNEL_ATTEMPT_FIELDS
    assert fields[reference], (
        f"{reference} constructs CrawlStrategyAttempt with no explicit fields; the gate "
        f"compares nothing and must fail rather than pass empty."
    )
    assert fields[other] == fields[reference], (
        f"{reference} and {other} no longer build the attempt with the same fields. "
        f"{reference}: {sorted(fields[reference])} {other}: {sorted(fields[other])}. "
        f"A field added to one terminal funnel and not the other silently diverges "
        f"the shared attempt shape -- add it to both in the same change."
    )


def test_attempt_constructor_mirror_detector_bites():
    """The funnel field-set comparison fires on a planted one-sided field."""
    both = (
        "def terminal_attempt(**kw):\n"
        "    return CrawlStrategyAttempt(strategy=None, status='failed', "
        "classification='x', http_status=None, content_type=None, elapsed_ms=0, error=None)\n"
        "def terminal_engine_result(**kw):\n"
        "    return CrawlStrategyAttempt(strategy=None, status='failed', "
        "classification='x', http_status=None, content_type=None, elapsed_ms=0, error=None)\n"
    )
    clean_tree = ast.parse(both)
    clean_fields = {
        spec.split(":", 1)[1]: _constructor_keywords(
            clean_tree, spec.split(":", 1)[1], "CrawlStrategyAttempt"
        )
        for spec in _FUNNEL_ATTEMPT_FIELDS
    }
    assert clean_fields["terminal_attempt"] == clean_fields["terminal_engine_result"]

    drifted = both.replace(
        "error=None)\n",
        "error=None, drifted_field=1)\n",
        1,
    )
    drifted_tree = ast.parse(drifted)
    first = _constructor_keywords(drifted_tree, "terminal_attempt", "CrawlStrategyAttempt")
    second = _constructor_keywords(drifted_tree, "terminal_engine_result", "CrawlStrategyAttempt")
    assert first != second and "drifted_field" in (first ^ second)


def test_the_internal_and_wire_taxonomies_name_the_same_members():
    """The engine's result records and the wire enums agree, set for set.

    That makes the adapters convert, not coerce; the fallback substitutes are the
    shape of the bug this whole change removed. All three axes are checked.
    """
    from app.models.crawl_status import CrawlStatus
    from app.models.crawl_strategy import CrawlStrategy
    from app.models.taxonomy import Classification, CrawlStrategyName
    from app.vocabulary.taxonomy import CrawlClassification, ResultStatus

    for axis, internal, wire in (
        ("status", literal_members(ResultStatus), _taxonomy_members(CrawlStatus)),
        ("classification", literal_members(Classification), _taxonomy_members(CrawlClassification)),
        ("strategy", literal_members(CrawlStrategyName), _taxonomy_members(CrawlStrategy)),
    ):
        assert internal == wire, (
            f"the {axis} taxonomies disagree: "
            f"internal-only={sorted(internal - wire)} wire-only={sorted(wire - internal)}. "
            f"app/api/response_shaping.py converts this axis without a fallback branch, "
            f"which is only true while the sets are equal -- so add the member to both sides "
            f"in the same change."
        )
