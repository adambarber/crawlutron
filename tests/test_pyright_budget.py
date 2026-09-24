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
satisfy the checker would have silently routed every streaming fetch through the
httpx fallback, because the import sits inside
`try: ... except ImportError: return _fallback_to_httpx(...)`.

The budget is an exact zero rather than a count, because the triage is finished:
there is no known-acceptable finding left, so any new one is either a real defect
or a suppression that has to be argued for in pyrightconfig.json. A count would
only be a number to ratchet, and this one did not need one.

Skipped when pyright is absent, so a runtime-only install does not fail the
suite on a development tool it was never given.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

SERVICE_ROOT = pathlib.Path(__file__).resolve().parent.parent

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
BUDGET = 1


def _strip_jsonc(text: str) -> str:
    """pyrightconfig.json is JSONC, so `json.loads` cannot read it directly."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("//"))


def _pyright() -> str | None:
    candidate = SERVICE_ROOT / ".venv" / "bin" / "pyright"
    if candidate.exists():
        return str(candidate)
    return shutil.which("pyright")


def test_pyright_whole_tree_is_clean() -> None:
    executable = _pyright()
    if executable is None:
        pytest.skip("pyright is not installed; install the dev requirements")

    completed = subprocess.run(
        [
            executable,
            "--pythonpath",
            str(SERVICE_ROOT / ".venv" / "bin" / "python"),
            "--outputjson",
        ],
        cwd=SERVICE_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode in (0, 1), f"pyright failed to run:\n{completed.stderr}"

    report = json.loads(completed.stdout)
    diagnostics = report["generalDiagnostics"]
    assert len(diagnostics) <= BUDGET, (
        f"{len(diagnostics)} pyright findings, budget {BUDGET}.\n"
        f"Each is either an annotation lie worth fixing or a checker limitation "
        f"worth arguing in pyrightconfig.json -- a third option, adjusting the "
        f"code to satisfy a finding that was wrong, is how the curl_cffi import "
        f"nearly became a silent httpx fallback.\n"
        + "\n".join(
            f"  {item['file'].split('browser-crawler/')[-1]}"
            f":{item['range']['start']['line'] + 1}  "
            f"{item.get('rule', '?')}: {item['message'].splitlines()[0]}"
            for item in diagnostics
        )
    )


def test_pyright_budget_is_tight() -> None:
    """A budget above the measurement is a standing allowance, not a ratchet."""
    executable = _pyright()
    if executable is None:
        pytest.skip("pyright is not installed; install the dev requirements")
    completed = subprocess.run(
        [executable, "--pythonpath", str(SERVICE_ROOT / ".venv" / "bin" / "python"), "--outputjson"],
        cwd=SERVICE_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    report = json.loads(completed.stdout)
    actual = len(report["generalDiagnostics"])
    assert BUDGET <= actual, (
        f"the pyright budget of {BUDGET} is stale -- only {actual} findings remain. "
        f"Tighten it to {actual} in this file, and to 0 once engine.py's two "
        f"regex.Pattern / re.Pattern lies are fixed."
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
