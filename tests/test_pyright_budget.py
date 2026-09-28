"""Whole-tree pyright budget: the tree's type-annotation lies must be zero.

Mypy is the declared type gate and is clean. This is the third checker, and it
exists because it finds things mypy structurally cannot: mypy reads the
`regex` package as untyped and degrades its values to `Any`, so a `regex.Pattern`
handed to a parameter declared `re.Pattern[str]` passes. Pyright found that, plus
a symbol imported from the wrong module and a `urllib.error` reference that only
resolved because some other import happened to bind the submodule.

Fourteen findings on first run, triaged against ground truth rather than taken
at face value: four were real defects and are fixed, and ten were the checker
being wrong -- six pydantic `default_factory` overloads, two incomplete lxml
stubs, one `curl_cffi` re-export marker, and one Literal-annotated property that
pyright widens. Verifying those ten mattered: "fixing" the `curl_cffi` import to
satisfy the checker would have silently routed every streaming fetch through
the httpx fallback, because the import sits inside the guarded
`try: ... except ImportError: return None` at the head of the transport, and
the plain-httpx answer is a real path, not an error.

The budget is an exact zero rather than a count, because the triage is finished:
there is no known-acceptable finding left, so any new one is either a real defect
or a suppression that has to be argued for in pyrightconfig.json. A count would
only be a number to ratchet, and this one did not need one.

This gate FAILS when pyright is absent. It used to skip, and that was how a
green result came to mean nothing: CI installs `.[dev]`, pyright was only ever
in requirements-dev.txt, which no workflow reads, so the pre-commit hook
printed "pyright budget ... Passed" while both substantive checks skipped. A
gate that cannot tell "clean" from "not run" is not a gate, so the tool is now
a declared dev dependency and its absence is an error.
"""

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
from chokepoint_walks import app_trees, assert_fires_once, assert_stays_clean, assignments, nodes_of_type

SERVICE_ROOT = pathlib.Path(__file__).resolve().parent.parent
_THIS_MODULE = sys.modules[__name__]

# Fourteen on first run. Eleven were fixed or suppressed with a documented
# reason. The four that remain are:
#
#   2  engine.py       the regex.Pattern / re.Pattern lie. Both sides of the
#                      diagnostic render as `Pattern[str] | None`, which is why
#                      mypy -- reading `regex` as untyped and degrading it to
#                      Any -- cannot see it. engine.py is mid-split in wave 1.
#   1  browser_pool    a FALSE POSITIVE, verified with a reveal_type probe on a
#                      `BrowserPool` parameter: pyright reports the property as
#                      the exact four-member Literal, so the value reaching the
#                      return is a declared member. It mis-resolves `self._pool`
#                      at that one site, and does not honour a per-line
#                      `# pyright: ignore[reportReturnType]` there. Left in the
#                      budget deliberately rather than worked around in code: the
#                      alternative is restructuring a lazy property that must stay
#                      lazy, because reading it before the pool initializes is the
#                      path that labels a worker which failed before acquire.
#   1  engine.py       counted above; two sites, one root cause.
#
# engine.py's two clear the moment wave 1's split lands, and the tight-budget
# test below fails the moment they do, which is how every other ratchet here
# closes. Zero is the target and nothing else is acceptable at the end.
# Fourteen on first run. Thirteen were fixed or suppressed with a documented
# reason. The fourteenth was misdiagnosed twice before it was fixed correctly,
# and the way it was misdiagnosed is the part worth keeping:
#
# It was first called a false positive on the strength of a `reveal_type` probe
# that showed the `browser_strategy` PROPERTY is the exact four-member Literal.
# That was true, and it said nothing about the other branch. The real defect was
# the constructor: `strategy` is annotated `CrawlStrategyName`, which includes
# the two HTTP strategies, so a browser worker could be constructed with
# `HTTP_DECLARED_CRAWLER` and emit it as a browser result's `selected_strategy`.
# A name outside the enum was worse -- accepted at construction, rejected much
# later by `CrawlResult.__post_init__` as a ValueError from a property nobody
# reading that constructor would predict. An annotation is not a runtime check.
#
# So: probing one branch of a function and generalising to the whole function is
# how a real defect gets filed as a tool bug. The budget is zero and the gate
# stays at zero.
#
# The last eight suppressions are gone, and so is the reason this file needed a
# second question asked of it. Not one of the eight had been suppressing anything:
# five in `app/api_models.py` (a worktree at 7d0aed1, the commit that added them,
# reports zero `reportArgumentType` findings there), two on the `lxml.etree`
# imports (quiet because `reportPrivateImportUsage` is off, not because lxml ships
# partial stubs), and one on `browser_pool._configured_strategy` (quiet since the
# worker constructor began validating the strategy it is handed). One of the
# eight was not a suppression at all but a prose sentence quoting one.
#
# That is a whole class of finding this budget could not see, because a
# suppression that suppresses nothing leaves the count at zero and the
# explanation still attached. Two rules in pyrightconfig.json make it visible,
# and test_pyright_detects_its_own_suppressions_decaying asserts they are on --
# with either off, the tree looks identical from here.
BUDGET = 0


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

    A skip here is indistinguishable from a pass in a pre-commit hook's output
    and in a CI log's green tick, which is the whole reason this function
    exists: the budget is a gate, and a gate that reports success without
    running has to be the loudest thing in the file.
    """
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

    Splitting on the service's own directory name assumed the service is nested
    under a parent directory of that name, which stops being true the moment it
    is its own repository -- and then every reported finding in a failure
    message shows an absolute path from whatever machine ran it.
    """
    try:
        return pathlib.Path(path).resolve().relative_to(SERVICE_ROOT).as_posix()
    except ValueError:
        return pathlib.Path(path).name


def _interpreter() -> str:
    """The interpreter pyright should resolve imports against.

    CI creates no `.venv` -- it installs the package into the runner's Python --
    so pointing pyright at a venv that is not there made the gate depend on a
    path that only exists locally.
    """
    venv_python = SERVICE_ROOT / ".venv" / "bin" / "python"
    return str(venv_python if venv_python.exists() else pathlib.Path(sys.executable))


def test_pyright_is_declared_in_the_dev_extra_ci_installs() -> None:
    """requirements-dev.txt is not read by any workflow, so it is not a source.

    `pip install -e ".[dev]"` is what CI runs, so a tool that is only in
    requirements-dev.txt is absent in CI and the gate that needs it is the one
    thing that cannot tell.
    """
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

    The two tests asked the same question of the same unchanged tree with the
    same command, and paid for the analysis twice: 2.29s and 2.20s, the two
    most expensive entries in this file. That is a962c7b's argument applied
    where it was not applied: one observation, handed to every reader, instead
    of one observation per reader.

    Memoized rather than computed at import, so running either test alone still
    runs pyright. Cached per session, not per file, because the answer is a
    property of the tree: if the tree changed mid-session the two assertions
    would have disagreed, and disagreement is the failure mode, not a feature.
    """
    executable = _require_pyright()
    completed = subprocess.run(
        [executable, "--pythonpath", _interpreter(), "--outputjson"],
        cwd=SERVICE_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode, completed.stdout


def test_pyright_whole_tree_is_clean() -> None:
    returncode, stdout = _pyright_report()
    assert returncode in (0, 1), f"pyright failed to run with status {returncode}"

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
    """Both rules that catch a suppression outliving its finding must be ON.

    Assertions about the config, not the tree, because the failure they guard is
    invisible FROM the tree: with either rule off, a suppression that stopped
    suppressing anything leaves this budget at zero and the suite green. That is
    not hypothetical. Eight suppressions sat in `app/` with comments explaining
    why they were needed and nothing to check any of it, and five of them had
    never suppressed anything -- from the commit that added them onward.

    `enableTypeIgnoreComments` is the other half of the same defect. Pyright
    honoring mypy's comments let four `# type: ignore[attr-defined]` in
    http_client_pool.py satisfy mypy while pyright's own view of those same four
    lines reported `Cannot assign to attribute "_bound_loop"`, and a bare
    `# type: ignore[return-value]` in cgroup_sense.py hid a real `Type "object" is
    not assignable to return type "str | None"`. Two checkers, one tree, and every
    one of those findings visible to only one of them.
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


# ---------------------------------------------------------------------------
# Keeping pyright's view of the models TRUE.
#
# This is not another check for annotation lies; it protects the checker itself.
# Pyright 1.1.406 synthesises a pydantic model's __init__ from the annotation
# and the assigned value, and reads a positional Field default as NO default --
# so `etag: str | None = Field(None, description=...)` was reported as a
# REQUIRED parameter while the interpreter accepted the call without it.
# Verified directly rather than inferred: a probe constructing
# `UnifiedCrawlResult(original_url=..., status=..., classification=...)` drew
# fifteen "Arguments missing" errors from a model where thirty-two of its fields
# have defaults.
#
# The consequence was architectural, not cosmetic. A direct `Model(...)` call
# drew a wall of false reportCallIssue errors and a dict literal drew none,
# because nothing type-checks a dict against a model -- so model construction
# routed through `dict[str, Any]` throughout the service. The dicts were the
# load-bearing workaround for a checker that misreported the signature, and a
# dict is exactly where a key can be dropped without a trace. That is how
# `metadata={}` shipped a /v1/render whose PageMetadata.title no input could
# fill: four of the six keys the browser worker produced were filtered away by
# name, and every field of the model has a default, so a dict omitting one was a
# valid dict.
#
# The declared default therefore has to sit where a checker can see it:
# `Annotated[T, Field(description=...)] = default`, or a bare `= default` when
# the Field call carries nothing else. Both produce a byte-identical OpenAPI
# schema, which is why this is a pure type-fidelity change and not a wire change.
#
# Without this gate, one comfortable `Field(None, description=...)` re-breaks the
# checker's view of every model in the service and floods a BUDGET=0 gate with
# findings that are all wrong -- the state this codebase was in, with six
# `# pyright: ignore` suppressions in pyrightconfig.json's shadow.
# ---------------------------------------------------------------------------


def _annotated_field_calls(annotation: ast.expr | None) -> Iterator[ast.Call]:
    """Every ``Field(...)`` written as metadata inside an ``Annotated[...]``.

    The half of the pattern space the detector was blind to, and in this file the
    larger half. ``x: Annotated[T, Field(<default>, ...)] = <default>`` states its
    default twice and puts it in the slot a checker cannot see; reading only the
    assignment's value missed every one of them, including two ``url``
    re-declarations in ``app/api_models.py`` written exactly this way, one of them
    carrying a comment that said it was inherited. A detector that sees one of two
    spellings of a shape is not a detector of the shape.
    """
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

    Ellipsis (``Field(..., ...)``) is excluded because those fields really are
    required and pyright is right about them. ``default_factory=`` is excluded
    because pyright resolves the factory, so it is a different question from a
    default pyright cannot see.

    Both spellings are swept -- the default in the assignment's value
    (``x: T = Field(None, ...)``) and in the annotation's ``Annotated`` metadata
    (``x: Annotated[T, Field(None, ...)] = None``) -- because they are one rule,
    and a detector that reads only one of them reports a clean tree over the
    other without ever saying so.
    """
    found: list[str] = []
    where = f"{path.name}:" if path is not None else ""
    for node, target, value in assignments(nodes_of_type(tree, ast.AST)):
        candidates: list[ast.expr] = [value] if isinstance(value, ast.Call) else []
        candidates.extend(_annotated_field_calls(node.annotation if isinstance(node, ast.AnnAssign) else None))
        for call in candidates:
            if not (isinstance(call.func, ast.Name) and call.func.id == "Field"):
                continue
            if any(key.arg == "default_factory" for key in call.keywords):
                continue
            if call.args:
                default = call.args[0]
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
    # The spelling the detector used to be blind to: the default sits inside the
    # Annotated metadata while the assignment repeats it, so one default is stated
    # twice and either statement can be edited alone.
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
            # The form the fix produces: the default sits where pyright reads it,
            # on the assignment, stated once.
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
            # default_factory is resolved by pyright, so it is a different
            # question and must not be swept up in this one.
            "from pydantic import BaseModel, Field\n"
            "class M(BaseModel):\n"
            "    links: list[str] = Field(default_factory=list, description='d')\n",
            "class M:\n    a = 1\n",
        ),
        path=pathlib.Path("bites.py"),
    )
