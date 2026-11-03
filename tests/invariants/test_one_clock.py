"""The scheduler package reads ONE clock: the injected one (#580).

#580: cooldowns stamped wall time while breakers ran monotonic -- three clock
families in one state object, and a partially dead ``clock=`` knob. This rule
forecloses the category: no ``time.*`` call anywhere in the two scheduler
modules except the sanctioned constructor clock defaults.
"""

from __future__ import annotations

import ast
import pathlib

from tests.chokepoint_walks import REPO_ROOT, attribute_calls, functions, offender_at
from tests.invariants._proof_paths import ELSEWHERE as _ELSEWHERE
from tests.rules import Bites, Registry, Rule, Silent

# Derived from the tree, not a hardcoded tuple: a scheduler sibling module is
# governed the moment it exists (adversarial review of #580, hole 4).
_SCHEDULER_PACKAGE = tuple((REPO_ROOT / "crawlutron" / "transport").glob("scheduler*.py"))


def _dotted(node: ast.expr | None) -> str | None:
    """``time.monotonic`` as a dotted string; None for anything else."""
    if not isinstance(node, ast.Attribute):
        return None
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _has_clock_default(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Whether ``fn`` declares any parameter defaulting to time.monotonic."""
    defaults = (fn.args.kw_defaults or []) + list(fn.args.defaults)
    return any(_dotted(default) == "time.monotonic" for default in defaults)


def _all_time_offenders(tree: ast.Module, path: pathlib.Path) -> list[str]:
    """Every clock read the package's ONE-timeline law refuses (#580).

    Three sweeps in one detector so the accounting audit sees one wired walk:
    raw time.* attribute calls (the sanctioned clock defaults exempt), clock
    defaults of the wrong family, and bare-name calls via from-imports.
    """
    if path not in _SCHEDULER_PACKAGE:
        return
    for fn in functions(tree):
        sanctioned_default = _has_clock_default(fn)
        for call, attr in attribute_calls(fn):
            dotted = _dotted(attr)
            if dotted is None or not dotted.startswith("time."):
                continue
            if dotted == "time.monotonic" and sanctioned_default:
                continue
            yield offender_at(
                path,
                call,
                f"{fn.name}() calls {dotted} directly: every timestamp in the scheduler "
                f"flows through the injected clock (#580)",
            )
        for default in (fn.args.kw_defaults or []) + list(fn.args.defaults):
            dotted = _dotted(default)
            if dotted is not None and dotted.startswith("time.") and dotted != "time.monotonic":
                yield offender_at(
                    path,
                    default,
                    f"clock default is {dotted}: the scheduler's one timeline is monotonic; "
                    f"a wall default is the #580 defect",
                )
    imported: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "time":
            imported.update(alias.asname or alias.name for alias in node.names if alias.name != "*")
    for fn in functions(tree):
        for call in ast.walk(fn):
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id in imported:
                yield offender_at(
                    path,
                    call,
                    f"{fn.name}() calls {call.func.id}() via a from-import: spell it time.* so "
                    f"the one-clock detector sees it, or inject the clock (#580)",
                )


THE_SCHEDULER_PACKAGE_READS_ONE_INJECTED_CLOCK = Rule(
    name="the-scheduler-package-reads-one-injected-clock",
    find=_all_time_offenders,
    summary=(
        "no time.* call in the scheduler modules outside the sanctioned clock "
        "defaults: the injected monotonic clock is the package's single "
        "timeline, and a second family is the #580 defect"
    ),
    bite=lambda: (
        Bites(
            "import time\n\nclass DomainScheduler:\n    def run_one(self, job):\n        now = time.time()\n",
            "calls time.time directly",
            at=REPO_ROOT / "crawlutron" / "transport" / "scheduler.py",
        ),
        Bites(
            "import time\n\n"
            "def breaker_remaining(self, *, now):\n"
            "    return max(0.0, self.circuit_open_until - time.monotonic())\n",
            "calls time.monotonic directly",
            at=REPO_ROOT / "crawlutron" / "transport" / "scheduler_domain_state.py",
        ),
        # The sanctioned default's FAMILY is pinned: a wall default is the
        # original defect in two tokens (adversarial review of #580).
        Bites(
            "import time\n\n"
            "class DomainScheduler:\n"
            "    def __init__(self, *, clock=time.time):\n"
            "        self._clock = clock\n",
            "clock default is time.time",
            at=REPO_ROOT / "crawlutron" / "transport" / "scheduler.py",
        ),
        # A from-import reads the clock with no attribute node: the attribute
        # sweep alone is blind to it (adversarial review of #580).
        Bites(
            "from time import monotonic\n\ndef remaining(self):\n    return monotonic()\n",
            "via a from-import",
            at=REPO_ROOT / "crawlutron" / "transport" / "scheduler_domain_state.py",
        ),
        # ...and the ALIASED shape binds under asname, not name (adversarial
        # review of #580, second pass: the first detector fix missed it).
        Bites(
            "from time import monotonic as mono\n\ndef remaining(self):\n    return mono()\n",
            "via a from-import",
            at=REPO_ROOT / "crawlutron" / "transport" / "scheduler_domain_state.py",
        ),
    ),
    clean=lambda: (
        # The detector is path-keyed: the same source placed elsewhere must
        # stay quiet, proving the path gate actually gates.
        Silent(
            "import time\n\n"
            "class DomainScheduler:\n"
            "    def __init__(self, *, clock=time.monotonic):\n"
            "        self._clock = clock\n\n"
            "    def run_one(self, job):\n"
            "        now = self._clock()\n",
            at=_ELSEWHERE,
        ),
    ),
)

RULES = Registry((THE_SCHEDULER_PACKAGE_READS_ONE_INJECTED_CLOCK,))


def test_no_rule_violations():
    """The scheduler modules read only the injected clock (#580)."""
    RULES.check_all_clean()
