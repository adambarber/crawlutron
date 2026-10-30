"""Whole-tree pyright budget: the tree's type-annotation lies must be zero.

Pyright finds what mypy structurally cannot (untyped ``regex`` interop,
wrong-module imports); findings are triaged against ground truth."""

from __future__ import annotations

import ast
import json
import pathlib
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Iterator
from functools import cache

import pytest

from tests.chokepoint_walks import (
    app_trees,
    assert_fires_once,
    assert_stays_clean,
    assignments,
    nodes_of,
)

SERVICE_ROOT = pathlib.Path(__file__).resolve().parent.parent
_THIS_MODULE = sys.modules[__name__]

# Fourteen on first run. The four that remain are: 2 engine.py the regex.Pattern / re.Pattern lie.
BUDGET = 0

# The whole-tree analysis tests are ``slow``: a checker subprocess is not a fast
# behavioral test. The gate is the pre-commit hook, which runs this file whole.
PYRIGHT_TIMEOUT_SECONDS = 600.0


def _strip_jsonc(text: str) -> str:
    """pyrightconfig.json is JSONC, so `json.loads` cannot read it directly."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("//"))


def _pyright() -> str | None:
    candidate = SERVICE_ROOT / ".venv" / "bin" / "pyright"
    if candidate.exists():
        return str(candidate)
    return shutil.which("pyright")


def _require_pyright() -> str:
    """The tool, or a failure. Never a skip.

    A skip is indistinguishable from a pass in hook and CI output -- a gate
    that reports success without running has to be the loudest thing in it."""
    executable = _pyright()
    if executable is None:
        pytest.fail(
            "pyright is not installed, so this gate proved nothing: the budget is "
            "asserted against no analysis at all. It is declared in the dev extra "
            "that CI installs, so this means the environment is wrong, not that "
            "the tree is clean. Install it with `pip install -e .[dev]`."
        )
    return executable


def _readable(path: str) -> str:
    """A finding's path relative to the service root, wherever pyright ran from.

    Splitting on the service's directory name assumed a parent of that name,
    which fails the moment the service is its own repository."""
    try:
        return pathlib.Path(path).resolve().relative_to(SERVICE_ROOT).as_posix()
    except ValueError:
        return pathlib.Path(path).name


def _interpreter() -> str:
    """The interpreter pyright should resolve imports against.

    CI creates no ``.venv`` -- it installs into the runner's Python -- so a venv
    path that only exists locally made the gate environment-dependent."""
    venv_python = SERVICE_ROOT / ".venv" / "bin" / "python"
    return str(venv_python if venv_python.exists() else pathlib.Path(sys.executable))


def test_pyright_is_declared_in_the_dev_extra_ci_installs() -> None:
    """requirements-dev.txt is read only by the Dockerfile test stage, not CI.

    CI runs ``pip install -e ".[dev]"``, so a tool only in requirements-dev.txt
    is absent where the gate needs it."""
    pyproject = tomllib.loads((SERVICE_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dev = pyproject["project"]["optional-dependencies"]["dev"]
    pinned = [entry for entry in dev if entry.split("==")[0].strip() == "pyright"]
    assert pinned, f"pyright is not in the dev extra CI installs: {dev}"
    assert "==" in pinned[0], f"pyright must be pinned, like every other gate tool: {pinned[0]!r}"


def test_a_missing_pyright_is_a_failure_not_a_skip(monkeypatch) -> None:
    monkeypatch.setattr(_THIS_MODULE, "_pyright", lambda: None)
    with pytest.raises(pytest.fail.Exception):
        _require_pyright()


def test_the_gate_resolves_the_interpreter_that_actually_ran_it() -> None:
    """A venv path that only exists locally is not a resolution strategy."""
    resolved = pathlib.Path(_interpreter())
    assert resolved.exists(), f"pyright would be pointed at a missing interpreter: {resolved}"


@cache
def _pyright_report() -> tuple[int, str]:
    """One pyright run of the whole tree, shared by both assertions below.

    The two tests paid for the same analysis twice; memoized per session (not
    at import), so either test alone still runs pyright."""
    executable = _require_pyright()
    completed = subprocess.run(
        [executable, "--pythonpath", _interpreter(), "--outputjson"],
        cwd=SERVICE_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=PYRIGHT_TIMEOUT_SECONDS,
    )
    return completed.returncode, completed.stdout


@pytest.mark.slow
def test_pyright_whole_tree_is_clean() -> None:
    returncode, stdout = _pyright_report()
    assert returncode in (0, 1), (
        f"pyright failed to run with status {returncode}.\n"
        f"It is a whole-tree analysis in a separate process: on a machine under "
        f"load the checker itself is killed, and a budget gate that reports a "
        f"code failure it did not find is lying about what it proved."
    )

    report = json.loads(stdout)
    diagnostics = report["generalDiagnostics"]
    assert len(diagnostics) <= BUDGET, (
        f"{len(diagnostics)} pyright findings, budget {BUDGET}.\n"
        f"Each is either an annotation lie worth fixing or a checker limitation "
        f"worth arguing in pyrightconfig.json -- a third option, adjusting the "
        f"code to satisfy a finding that was wrong, is how the curl_cffi import "
        f"nearly became a silent httpx fallback.\n"
        + "\n".join(
            f"  {_readable(item['file'])}"
            f":{item['range']['start']['line'] + 1}  "
            f"{item.get('rule', '?')}: {item['message'].splitlines()[0]}"
            for item in diagnostics
        )
    )


@pytest.mark.slow
def test_pyright_budget_is_tight() -> None:
    """A budget above the measurement is a standing allowance, not a ratchet."""
    _returncode, stdout = _pyright_report()
    report = json.loads(stdout)
    actual = len(report["generalDiagnostics"])
    assert BUDGET <= actual, (
        f"the pyright budget of {BUDGET} is stale -- only {actual} findings remain. "
        f"Tighten it in this file. Zero is the target and there is no allowance "
        f"left in it."
    )


def test_pyright_detects_its_own_suppressions_decaying() -> None:
    """Both rules that catch a suppression outliving its finding must be ON: with either
    off, a decaying suppression leaves the budget at zero and the suite green -- five of
    eight parked suppressions had never suppressed anything.
    """
    config = json.loads(_strip_jsonc((SERVICE_ROOT / "pyrightconfig.json").read_text(encoding="utf-8")))
    assert config.get("enableTypeIgnoreComments") is False, (
        "pyright is honoring mypy's `# type: ignore` comments again, so a "
        "suppression that satisfies the fail gate can hide a finding from the "
        "checker whose whole job is annotation lies. Mypy owns its own comments "
        "(`warn_unused_ignores` is on in mypy.ini); pyright must not speak for it."
    )
    assert config.get("reportUnnecessaryTypeIgnoreComment") == "error", (
        "pyright is not reporting suppressions that suppress nothing, so a "
        "suppression can outlive the finding it was written for with its "
        "explanation still attached and this budget still reading zero. It also "
        "catches the prose case: a comment quoting a suppression directive IS "
        "one, and mutes the line it sits on."
    )


def test_pyright_config_targets_the_running_interpreter() -> None:
    """A config nobody executes is documentation, not a gate.

    This one shipped pinned to Python 3.11 while the service runs 3.12, so it had
    never produced a finding against the interpreter the tests actually use.
    """
    import sys

    config = json.loads(_strip_jsonc((SERVICE_ROOT / "pyrightconfig.json").read_text(encoding="utf-8")))
    running = f"{sys.version_info.major}.{sys.version_info.minor}"
    assert config.get("pythonVersion") == running, (
        f"pyrightconfig.json targets Python {config.get('pythonVersion')}, but the "
        f"service venv is {running}. A type check against the wrong language "
        f"version is not a type check."
    )


def test_pyright_config_include_points_at_a_live_tracked_package() -> None:
    """The whole-tree analysis must analyze the tree, or it passes empty.

    The app-to-crawlutron rename repointed every gate config except this one,
    leaving `include` pointing at a directory with zero tracked files -- pyright
    reported filesAnalyzed: 0 and the budget gate passed trivially against no
    analysis at all. This pin makes that shape a build failure.
    """
    config = json.loads(_strip_jsonc((SERVICE_ROOT / "pyrightconfig.json").read_text(encoding="utf-8")))
    includes = config.get("include", [])
    assert includes, f"pyrightconfig.json declares no include paths: {includes}"

    git = shutil.which("git")
    assert git, "git is required to prove the include path is a tracked package"
    is_tracked = subprocess.run(
        [git, "-C", str(SERVICE_ROOT), "ls-files"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()

    resolved = []
    for entry in includes:
        root = SERVICE_ROOT / entry
        if not root.exists():
            continue
        tracked_here = [f for f in is_tracked if f.startswith(entry.rstrip("/") + "/") and f.endswith(".py")]
        resolved.append((entry, len(tracked_here)))
    live = [(entry, n) for entry, n in resolved if n > 0]
    assert live, (
        "no include path in pyrightconfig.json resolves to tracked .py files: "
        f"{resolved}. An include that matches nothing lets the whole-tree "
        "analysis pass with filesAnalyzed: 0 -- a gate that analyses nothing "
        "proves nothing."
    )


# --------------------------------------------------------------------------- Keeping pyright's view of the
# models TRUE. This is not another check for annotation lies; it protects the checker itself.


def _annotated_field_calls(annotation: ast.expr | None) -> Iterator[ast.Call]:
    """Every ``Field(...)`` written as metadata inside an ``Annotated[...]``.

    The half the detector was blind to, and here the larger one: the default is
    stated twice, in the slot a checker cannot see."""
    if not (isinstance(annotation, ast.Subscript) and isinstance(annotation.value, ast.Name)):
        return
    if annotation.value.id != "Annotated":
        return
    items = annotation.slice.elts if isinstance(annotation.slice, ast.Tuple) else [annotation.slice]
    for item in items:
        if isinstance(item, ast.Call) and isinstance(item.func, ast.Name) and item.func.id == "Field":
            yield item


def _positional_field_default_offenders(tree: ast.Module, path: pathlib.Path | None = None) -> list[str]:
    """``Field(<default>, ...)`` sites, wherever the ``Field`` is written.

    Ellipsis excluded (really-required); ``default_factory=`` excluded
    (pyright resolves it); both spellings swept -- they are one rule."""
    found: list[str] = []
    where = f"{path.name}:" if path is not None else ""
    for node, target, value in assignments(nodes_of(tree)):
        candidates: list[ast.Call] = [value] if isinstance(value, ast.Call) else []
        annotation = node.annotation if isinstance(node, ast.AnnAssign) else None
        candidates.extend(_annotated_field_calls(annotation))
        for call in candidates:
            if not (isinstance(call.func, ast.Name) and call.func.id == "Field"):
                continue
            if any(key.arg == "default_factory" for key in call.keywords):
                continue
            if call.args:
                default: ast.expr | None = call.args[0]
            else:
                default = next((key.value for key in call.keywords if key.arg == "default"), None)
            if default is None or (isinstance(default, ast.Constant) and default.value is ...):
                continue
            found.append(
                f"{where}{ast.unparse(target)} declares its default inside Field(); pyright "
                f"reads that slot as no default, so the field looks required to every "
                f"constructor call -- and where the assignment repeats it, the two "
                f"statements of one default can drift. Declare it once, on the "
                f"assignment: Annotated[T, Field(...)] = <default>, or a bare = <default> "
                f"when Field carries nothing else."
            )
    return found


def test_pydantic_defaults_stay_where_a_checker_can_see_them() -> None:
    """Whole-tree, not just api_models.py: any module may declare a model."""
    offenders: list[str] = []
    for path, tree in app_trees():
        offenders.extend(_positional_field_default_offenders(tree, path))
    assert not offenders, (
        f"{len(offenders)} pydantic fields declare their default in a position "
        f"pyright cannot see, which makes them look required to every "
        f"constructor call and is what pushed this codebase's model construction "
        f"into untyped dicts.\n" + "\n".join(f"  {why}" for why in offenders)
    )


def test_pydantic_default_visibility_detector_bites() -> None:
    """The red half, and the clean half -- a detector that cannot fail is a lie."""
    positional = (
        "from pydantic import BaseModel, Field\n"
        "class M(BaseModel):\n"
        '    etag: str | None = Field(None, description="Origin ETag.")\n'
    )
    keyword = (
        "from pydantic import BaseModel, Field\n"
        "class M(BaseModel):\n"
        '    robots: bool = Field(default=True, description="Honor robots.txt.")\n'
    )
    inside_annotated = (
        "from typing import Annotated\n"
        "from pydantic import BaseModel, Field\n"
        "class M(BaseModel):\n"
        '    context: Annotated[str, Field("", description="Context.")] = ""\n'
    )
    assert_fires_once(
        _positional_field_default_offenders, {positional: "looks required"}, pathlib.Path("bites.py")
    )
    assert_fires_once(
        _positional_field_default_offenders, {keyword: "looks required"}, pathlib.Path("bites.py")
    )
    assert_fires_once(
        _positional_field_default_offenders, {inside_annotated: "can drift"}, pathlib.Path("bites.py")
    )
    assert_stays_clean(
        _positional_field_default_offenders,
        (
            # The form the fix produces: the default sits where pyright reads it, on the assignment, stated
            # once.
            "from typing import Annotated\n"
            "from pydantic import BaseModel, Field\n"
            "class M(BaseModel):\n"
            '    etag: Annotated[str | None, Field(description="Origin ETag.")] = None\n'
            '    context: Annotated[str, Field(description="Context.")] = ""\n'
            "    plain: str | None = None\n",
            # Ellipsis is really required, and pyright is right about it.
            "from pydantic import BaseModel, Field\n"
            "class M(BaseModel):\n"
            '    url: str = Field(..., description="Root URL.")\n',
            # default_factory is resolved by pyright, so it is a different question and must not be swept up
            # in this one.
            "from pydantic import BaseModel, Field\n"
            "class M(BaseModel):\n"
            "    links: list[str] = Field(default_factory=list, description='d')\n",
            "class M:\n    a = 1\n",
        ),
        path=pathlib.Path("bites.py"),
    )
