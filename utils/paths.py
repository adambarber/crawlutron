"""The one path door: caller-owned root, surfaces derived from the uv manifest.

No derivation here depends on where a script or a test file happens to live.
"""

from __future__ import annotations

import ast
import pathlib
import sys
import tomllib
from collections.abc import Callable, Mapping
from functools import cache
from types import MappingProxyType, ModuleType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    SCAN_ROOTS: tuple[str, ...]
    PACKAGE_ROOTS: tuple[str, ...]
    APP_ROOT: pathlib.Path
    ADMISSION_ROOT: pathlib.Path
    REST_ROOT: pathlib.Path
    MCP_ROOT: pathlib.Path
    SAFE_URL_ROOT: pathlib.Path
    APP_ROOTS: tuple[pathlib.Path, ...]
    TRANSPORT_DIR: pathlib.Path


def _root() -> pathlib.Path:
    """The door's own root, confirmed by the uv manifest: never the caller's cwd."""
    root = pathlib.Path(__file__).resolve().parents[1]
    if not (root / "pyproject.toml").is_file():
        raise RuntimeError(f"path door is installed outside a uv project: {root}")
    return root


REPO_ROOT = _root()
TESTS_DIR = REPO_ROOT / "tests"


@cache
def python_bearing_roots(base: pathlib.Path = REPO_ROOT) -> tuple[str, ...]:
    """Every top-level dir under ``base`` holding any ``*.py``, sorted. Hidden dirs and symlinked trees
    (venvs) are skipped -- caches and environments, not shipped code. Derived by walking, never
    hand-listed: the app/ rename and crawlutron_sdk's late arrival were hand-list blindness.
    """
    roots = [
        d.name
        for d in base.iterdir()
        if d.is_dir()
        and not d.is_symlink()
        and not d.name.startswith(".")
        and any("__pycache__" not in q.parts for q in d.rglob("*.py"))
    ]
    if not roots:
        raise AssertionError(f"no python-bearing roots under {base}: repo surface is empty")
    return tuple(sorted(roots))


def uv_workspace_members(base: pathlib.Path = REPO_ROOT) -> tuple[str, ...] | None:
    """``[tool.uv.workspace].members`` from the root pyproject, or None when absent.

    The packaging truth's key: when a UV workspace declares members, PACKAGE_ROOTS
    derives from them; until then the current-layout fallback stands."""
    pyproject = base / "pyproject.toml"
    if not pyproject.is_file():
        return None
    try:
        with pyproject.open("rb") as handle:
            table = tomllib.load(handle)
    except tomllib.TOMLDecodeError:
        return None
    tool = table.get("tool")
    if not isinstance(tool, dict):
        return None
    workspace = tool.get("uv", {}).get("workspace") if isinstance(tool.get("uv"), dict) else None
    if not isinstance(workspace, dict):
        return None
    members = workspace.get("members")
    if not isinstance(members, list):
        return None
    return tuple(str(m) for m in members if isinstance(m, str))


def _expanded_members(base: pathlib.Path, pattern: str) -> tuple[str, ...]:
    """A member pattern as concrete existing dirs, named repo-relatively.

    UV workspace members are gitignore-style globs (``packages/*``); a literal
    member path is its own match, while a glob resolves to every matching dir."""
    literal = base / pattern
    if literal.is_dir():
        return (pattern,)
    return tuple(sorted(p.relative_to(base).as_posix() for p in base.glob(pattern) if p.is_dir()))


def workspace_package_dirs(base: pathlib.Path = REPO_ROOT) -> Mapping[str, pathlib.Path]:
    """The root project plus every workspace member, keyed by base-relative path."""
    dirs: dict[str, pathlib.Path] = {".": base}
    for pattern in uv_workspace_members(base) or ():
        dirs.update({member: base / member for member in _expanded_members(base, pattern)})
    return MappingProxyType(dirs)


def workspace_project(directory: pathlib.Path) -> Mapping[str, object]:
    """A package's ``[project]`` table, keyed by string."""
    with (directory / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)["project"]
    if not isinstance(project, dict):
        raise AssertionError(f"[project] table is not a mapping in {directory}")
    return MappingProxyType({str(key): value for key, value in project.items()})


def _member_package_roots(base: pathlib.Path, members: tuple[str, ...]) -> tuple[str, ...]:
    """The python-bearing member dirs of a UV workspace, named repo-relatively.

    A member that holds any ``*.py`` is its own package root; a member with none
    is a stub (docs-only), not part of the packaging surface."""
    roots = [
        member
        for pattern in members
        for member in _expanded_members(base, pattern)
        if any("__pycache__" not in q.parts for q in (base / member).rglob("*.py"))
    ]
    if not roots:
        raise AssertionError(f"uv workspace members declare no python-bearing dirs under {base}: {members}")
    return tuple(sorted(set(roots)))


@cache
def package_roots(base: pathlib.Path = REPO_ROOT) -> tuple[str, ...]:
    """The importable package dirs (derived packaging truth), sorted.

    A UV workspace declaration keys it; without one, the current-layout fallback
    (every top-level python-bearing dir) stands until the Wave A manifest arrives."""
    members = uv_workspace_members(base)
    if members is not None:
        return _member_package_roots(base, members)
    return python_bearing_roots(base)


def _member_import_packages(base: pathlib.Path, member: str) -> tuple[str, ...]:
    """The import names a member's wheel ships, from its own ``packages`` list.

    ``crawlutron, admission`` for core, ``crawlutron_rest`` for rest; a member
    without a wheel target is a stub container and ships nothing.
    """
    pyproject = base / member / "pyproject.toml"
    if not pyproject.is_file():
        return ()
    try:
        with pyproject.open("rb") as handle:
            table = tomllib.load(handle)
    except tomllib.TOMLDecodeError:
        return ()
    wheel = table.get("tool", {}).get("hatch", {}).get("build", {}).get("targets", {}).get("wheel", {})
    packages = wheel.get("packages")
    if not isinstance(packages, list):
        return ()
    return tuple(str(p) for p in packages if isinstance(p, str))


@cache
def import_roots(base: pathlib.Path = REPO_ROOT) -> tuple[str, ...]:
    """The import-root package dirs (``packages/<member>/<import-name>``), derived.

    From each member's wheel ``packages`` truth; a new member lands here by its
    own pyproject, never a hand-typed list (#906). Memoized via @cache."""
    roots = [
        f"{member}/{pkg}" for member in package_roots(base) for pkg in _member_import_packages(base, member)
    ]
    if not roots:
        raise AssertionError(f"uv members declare no import roots under {base}")
    return tuple(sorted(set(roots)))


@cache
def member_src_roots(base: pathlib.Path = REPO_ROOT) -> tuple[pathlib.Path, ...]:
    """Every uv member's src dir, derived from the manifest, not hand-listed."""
    roots = [
        base / member / "src"
        for pattern in uv_workspace_members(base) or ()
        for member in _expanded_members(base, pattern)
        if (base / member / "src").is_dir()
    ]
    if not roots:
        raise AssertionError(f"uv members declare no src dirs under {base}")
    return tuple(sorted(roots))


@cache
def member_import_names(base: pathlib.Path = REPO_ROOT) -> tuple[str, ...]:
    """The import names the members ship, from each wheel's packages list."""
    names = [
        pathlib.PurePosixPath(pkg).name
        for member in package_roots(base)
        for pkg in _member_import_packages(base, member)
    ]
    if not names:
        raise AssertionError(f"uv members declare no import names under {base}")
    return tuple(sorted(set(names)))


def prepend_member_roots(base: pathlib.Path, search_path: list[str]) -> None:
    """Put this tree's member roots ahead of any editable install from another tree."""
    search_path[:0] = [str(root) for root in reversed(member_src_roots(base))]


def _holds_python(directory: pathlib.Path) -> bool:
    """True if a directory holds any code, ``__pycache__`` aside."""
    return directory.is_dir() and any("__pycache__" not in q.parts for q in directory.rglob("*.py"))


def get_test_roots(base: pathlib.Path = REPO_ROOT) -> tuple[str, ...]:
    """The python-bearing test homes: the flat ``tests`` dir plus each
    ``packages/<member>/tests`` home, named repo-relatively and sorted. Each
    home is first-class: named, never merely covered by a member root."""
    flat = ("tests",) if _holds_python(base / "tests") else ()
    member_homes = sorted(
        f"{member}/tests" for member in package_roots(base) if _holds_python(base / member / "tests")
    )
    return tuple(sorted(set(flat) | set(member_homes)))


def scan_roots(base: pathlib.Path = REPO_ROOT) -> tuple[str, ...]:
    """Every python-bearing dir under ``base``: package and top-level roots,
    minus the member containers a member root already covers. Per-package
    test homes go to :func:`get_test_roots` (a member root covers them, #908)."""
    packages = set(package_roots()) if base == REPO_ROOT else set(package_roots(base))
    package_paths = {pathlib.PurePosixPath(p) for p in packages}
    containers = {
        root
        for root in python_bearing_roots(base)
        if any(pathlib.PurePosixPath(root) in pkg.parents for pkg in package_paths)
    }
    return tuple(sorted(packages | (set(python_bearing_roots(base)) - containers)))


def is_exempt(relative: str, value: str, exemptions: tuple[tuple[str, str, str], ...]) -> bool:
    """True if a (path-pattern, allowed-substring) exemption row covers ``value``.
    Shared by the string-drift invariant gates so their ``_exempt`` is one home."""
    for pattern, allowed, _justification in exemptions:
        path = pathlib.PurePosixPath(relative)
        if path == pathlib.PurePosixPath(pattern) or path.match(pattern):
            if allowed in value:
                return True
    return False


def _import_root(name: str) -> pathlib.Path:
    """The src root of the uv member shipping import package ``name``."""
    hits = [REPO_ROOT / rel for rel in import_roots() if pathlib.PurePosixPath(rel).name == name]
    if len(hits) != 1:
        raise AssertionError(f"expected one uv member shipping {name!r}, got {hits}")
    return hits[0]


def _app_roots() -> tuple[pathlib.Path, pathlib.Path, pathlib.Path, pathlib.Path, pathlib.Path]:
    return (
        _import_root("crawlutron"),
        _import_root("admission"),
        _import_root("crawlutron_rest"),
        _import_root("crawlutron_mcp"),
        _import_root("safe_url"),
    )


_DERIVATIONS: Mapping[str, Callable[[], object]] = MappingProxyType(
    {
        "PACKAGE_ROOTS": package_roots,
        "SCAN_ROOTS": scan_roots,
        "APP_ROOT": lambda: _app_roots()[0],
        "ADMISSION_ROOT": lambda: _app_roots()[1],
        "REST_ROOT": lambda: _app_roots()[2],
        "MCP_ROOT": lambda: _app_roots()[3],
        "SAFE_URL_ROOT": lambda: _app_roots()[4],
        "APP_ROOTS": lambda: tuple(_app_roots()[i] for i in (0, 2, 3, 4)),
        "TRANSPORT_DIR": lambda: _app_roots()[0] / "transport",
    }
)


@cache
def _surface(name: str) -> object:
    """Derive a named surface once; the memo lives in the function, not module state."""
    return _DERIVATIONS[name]()


def __getattr__(name: str) -> object:
    """Serve every derived name through the same write-once cache."""
    if name not in _DERIVATIONS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return _surface(name)


class _FrozenModule(ModuleType):
    """A module whose name bindings are fixed once its surface is built."""

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_frozen", False):
            raise AttributeError(f"{self.__name__}.{name} is immutable")
        super().__setattr__(name, value)


def freeze_module(module: ModuleType) -> None:
    """Lock a module's names; every imported name is already derived and immutable."""
    module.__dict__["_frozen"] = True
    module.__class__ = _FrozenModule


def path_components(expr: ast.expr) -> list[ast.expr]:
    """Flatten ``a / b``, ``os.path.join(a, b)``, and ``Path(a)`` to components.

    Components stay expressions: the caller classifies them, never this helper.
    """
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Div):
        return [*path_components(expr.left), *path_components(expr.right)]
    if isinstance(expr, ast.Call):
        name = getattr(expr.func, "attr", None) or getattr(expr.func, "id", None)
        if name in {"Path", "PurePath"} and expr.args:
            return path_components(expr.args[0])
        if name == "join" and expr.args:
            return [part for arg in expr.args for part in path_components(arg)]
    return [expr]


def relative_posix(path: pathlib.Path) -> str:
    """The repo-relative path label a detector names a finding by."""
    return path.relative_to(REPO_ROOT).as_posix()


freeze_module(sys.modules[__name__])
