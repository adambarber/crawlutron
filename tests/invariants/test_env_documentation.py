"""Every knob .env.example documents is read somewhere, and every value it shows is the code's default."""

from __future__ import annotations

import ast
import re
from functools import lru_cache

from tests.chokepoint_walks import REPO_ROOT, parsed
from tests.invariants._env_knobs import _consumed_knobs


#: The .env.example lines that document a live knob (not commented-out examples).
def _documented_env() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in (REPO_ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        match = re.match(r"\s*(CRAWLUTRON_[A-Z0-9_]+)=(.*)", line)
        if match:
            values[match.group(1)] = match.group(2).strip()
    return values


def _app_and_scripts_sources() -> list[tuple[str, ast.Module]]:
    found: list[tuple[str, ast.Module]] = []
    for base in ("crawlutron", "scripts"):
        for path in sorted((REPO_ROOT / base).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            found.append((path.relative_to(REPO_ROOT).as_posix(), parsed(path)))
    return found


def _literal_number(node: ast.AST) -> int | float | str | bool | bytes | None:
    """A numeric/string/bool literal, or a simple ``48 * 1024 * 1024`` product."""
    if isinstance(node, ast.Constant):
        value = node.value
        return value if isinstance(value, (int, float, str, bool, bytes)) else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
        left, right = _literal_number(node.left), _literal_number(node.right)
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            return left * right
        return None
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        value = _literal_number(node.operand)
        return -value if isinstance(value, (int, float)) else None
    return None


_ENV_READER_ATTRS = frozenset(
    {
        "get",
        "getenv",
        "get_env_int",
        "get_env_float",
        "get_mapping_int",
        "get_mapping_float",
        "parse_bool_env",
        "parse_bool_value",
    }
)


def _reader_calls(sources: list[tuple[str, ast.Module]]) -> list[tuple[str, ast.Call]]:
    """Every (file, call) in these sources that goes through an env-reader face."""
    found: list[tuple[str, ast.Call]] = []
    for relative, tree in sources:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "attr", getattr(node.func, "id", "")) in _ENV_READER_ATTRS
            ):
                found.append((relative, node))
    return found


def _call_name_arg(call: ast.Call, name_index: int) -> str | None:
    """The string in a reader call's name slot, or None."""
    if len(call.args) > name_index:
        arg = call.args[name_index]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value
    return None


def _call_default_arg(call: ast.Call, name_index: int) -> int | float | str | bool | bytes | None:
    """The literal default argument of a reader call: positional after the name, or ``default=``."""
    default_node = call.args[name_index + 1] if len(call.args) > name_index + 1 else None
    for keyword in call.keywords:
        if keyword.arg == "default":
            default_node = keyword.value
    value = _literal_number(default_node) if default_node is not None else None
    return value if isinstance(value, (int, float, str, bool, bytes)) else None


def _default_site_index() -> dict[str, list[tuple[str, int, int | float | str | bool | bytes]]]:
    """Every knob's (file, line, default) literal read sites, in one tree walk."""
    index: dict[str, list[tuple[str, int, int | float | str | bool | bytes]]] = {}
    for relative, call in _reader_calls(_app_and_scripts_sources()):
        for name_index in (0, 1):
            knob = _call_name_arg(call, name_index)
            if knob is None:
                continue
            default = _call_default_arg(call, name_index)
            if default is not None:
                index.setdefault(knob, []).append((relative, call.lineno, default))
    return index


@lru_cache(maxsize=1)
def _default_site_index_cached() -> dict[str, list[tuple[str, int, int | float | str | bool | bytes]]]:
    """The walk is one cost per process, not one cost per knob (was ~11s of 23s)."""
    return _default_site_index()


def _code_default_sites(knob: str) -> list[tuple[str, int, int | float | str | bool | bytes]]:
    """Every (file, line, default) where crawlutron/ or scripts/ reads ``knob`` with a literal default."""
    return list(_default_site_index_cached().get(knob, ()))


def _parse_bool(raw: str) -> bool:
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _value_matches(raw: str, default: int | float | str | bool | bytes) -> bool:
    if isinstance(default, bool):
        return _parse_bool(raw) == default
    if isinstance(default, (int, float)):
        try:
            return type(default)(raw) == default
        except (ValueError, TypeError):
            return False
    return raw == default


def _dead_documented_knobs() -> list[str]:
    """Documented knobs no crawlutron/ or scripts/ module even mentions."""
    documented = set(_documented_env())
    mentioned: set[str] = set()
    for _relative, tree in _app_and_scripts_sources():
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in documented:
                mentioned.add(node.value)
    return sorted(documented - mentioned)


def _reader_names_in_calls(tree: ast.Module) -> set[str]:
    """Every string passed in a name slot of an env-reader call in this tree."""
    found: set[str] = set()
    for _relative, call in _reader_calls([("", tree)]):
        for name_index in (0, 1):
            name = _call_name_arg(call, name_index)
            if name is not None:
                found.add(name)
    return found


def _script_consumed_undocumented() -> list[str]:
    """Knobs scripts/ reads through a reader that .env.example does not document."""
    documented = set(_documented_env())
    found: set[str] = set()
    for relative, tree in _app_and_scripts_sources():
        if relative.startswith("scripts/"):
            found |= _reader_names_in_calls(tree)
    return sorted(name for name in found if name not in documented and name.startswith("CRAWLUTRON_"))


def _value_drift_offenders() -> list[str]:
    """Documented values that disagree with the code default at every read site."""
    offenders: list[str] = []
    for knob, raw in sorted(_documented_env().items()):
        for relative, line, default in _code_default_sites(knob):
            if not _value_matches(raw, default):
                offenders.append(
                    f"{knob}={raw} in .env.example but {relative}:{line} defaults to {default!r}"
                )
    return offenders


def test_every_documented_env_knob_is_read():
    assert not (dead := _dead_documented_knobs()), (
        "documented knobs nothing reads (a dead operator control): " + ", ".join(dead)
    )
    assert not (undocumented := _script_consumed_undocumented()), (
        "knobs scripts/ reads but .env.example never documents: " + ", ".join(undocumented)
    )
    # The name-coverage walk must still find real consumers, or an empty .env.example goes green.
    assert _consumed_knobs(), "the consumer walk stopped finding real knob reads"


def test_documented_env_values_match_code_defaults():
    assert not (drift := _value_drift_offenders()), (
        ".env.example values disagree with the code defaults:\n  " + "\n  ".join(drift)
    )
