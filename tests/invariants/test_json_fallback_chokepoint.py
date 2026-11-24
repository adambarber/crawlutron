"""Every json.dumps fallback must be the json_default door; frozen migrations are
the only narrowing, admitted by exact relative path and never by basename.
"""

from __future__ import annotations

import ast
import pathlib

from tests.chokepoint_paths import APP_ROOTS, REPO_ROOT, relative_posix
from tests.chokepoint_walks import (
    app_trees,
    call_nodes,
    offender_at,
    parsed_source,
    resolved_call_name,
    resolved_expr_name,
)

_MIGRATION_SUFFIX = pathlib.Path("storage/migrations/0003_crawl_jobs_split_accumulators.py")
_DECOY_PATH = REPO_ROOT / "tests" / "decoys" / _MIGRATION_SUFFIX.name
_MESSAGE = "json.dumps fallback outside the json_default door"


def _frozen_paths() -> frozenset[str]:
    """Exact relative paths of the frozen migrations the door admits."""
    return frozenset(
        relative_posix(root / _MIGRATION_SUFFIX) for root in APP_ROOTS if (root / _MIGRATION_SUFFIX).is_file()
    )


def _is_door(tree: ast.Module, value: ast.expr) -> bool:
    name = resolved_expr_name(tree, value)
    return name == "json_default" or name.endswith(".json_default")


def _fallback_offenders(tree: ast.Module, path: pathlib.Path) -> list[str]:
    if relative_posix(path) in _frozen_paths():
        return []
    offenders: list[str] = []
    for call in call_nodes(tree):
        if resolved_call_name(tree, call).rsplit(".", 1)[-1] != "dumps":
            continue
        for keyword in call.keywords:
            if keyword.arg == "default" and not _is_door(tree, keyword.value):
                offenders.append(offender_at(path, keyword.value, _MESSAGE))
            elif keyword.arg is None and isinstance(keyword.value, ast.Dict):
                for key, value in zip(keyword.value.keys, keyword.value.values, strict=True):
                    if isinstance(key, ast.Constant) and key.value == "default" and not _is_door(tree, value):
                        offenders.append(offender_at(path, value, _MESSAGE))
    return offenders


def test_every_json_fallback_goes_through_json_default() -> None:
    offenders = [
        offender
        for path, tree in app_trees(exclude=frozenset())
        for offender in _fallback_offenders(tree, path)
    ]
    assert not offenders, "json.dumps fallback drifted from the door: " + "; ".join(offenders)


def test_frozen_migration_narrowing_is_by_exact_relative_path() -> None:
    source = "import json\njson.dumps({}, default=str)\n"
    decoy = parsed_source(source, filename=str(_DECOY_PATH))
    assert _fallback_offenders(decoy, _DECOY_PATH) == [f"tests/decoys/{_MIGRATION_SUFFIX.name}:2 {_MESSAGE}"]
    frozen_paths = _frozen_paths()
    assert len(frozen_paths) == 1
    frozen = next(
        (path, tree) for path, tree in app_trees(exclude=frozenset()) if relative_posix(path) in frozen_paths
    )
    assert _fallback_offenders(frozen[1], frozen[0]) == []


def test_non_door_fallbacks_bite_by_file_and_line() -> None:
    for shape in ("str", "repr", "jsonify", "lambda _: 1"):
        source = f"import json\njson.dumps({{}}, default={shape})\n"
        tree = parsed_source(source, filename=str(_DECOY_PATH))
        assert _fallback_offenders(tree, _DECOY_PATH), shape
    alias = parsed_source(
        "import json\nfallback = str\njson.dumps({}, default=fallback)\n",
        filename=str(_DECOY_PATH),
    )
    assert _fallback_offenders(alias, _DECOY_PATH)
    spread = parsed_source(
        'import json\njson.dumps({}, **{"default": str})\n',
        filename=str(_DECOY_PATH),
    )
    assert _fallback_offenders(spread, _DECOY_PATH)


def test_the_door_name_and_import_alias_stay_clean() -> None:
    source = (
        "import json\nfrom crawlutron.safety.json_safe import json_default\n"
        "json.dumps({}, default=json_default)\n"
        "json.dumps({}, default=json_default)\n"
    )
    tree = parsed_source(source, filename=str(_DECOY_PATH))
    assert _fallback_offenders(tree, _DECOY_PATH) == []


def test_the_gate_walks_the_whole_app_surface() -> None:
    paths = [path for path, _ in app_trees(exclude=frozenset())]
    assert any(relative_posix(path).endswith("safety/json_safe.py") for path in paths)
