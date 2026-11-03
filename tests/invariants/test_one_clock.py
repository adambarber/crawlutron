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

_SCHEDULER_PACKAGE = (
    REPO_ROOT / "crawlutron" / "transport" / "scheduler.py",
    REPO_ROOT / "crawlutron" / "transport" / "scheduler_domain_state.py",
)


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


def _raw_time_reads(tree: ast.Module, path: pathlib.Path) -> list[str]:
    """Every ``time.<anything>(...)`` call in a scheduler-package module.

    The sanctioned clock defaults are the only carve-out: the constructor
    signatures declare them, and this detector refuses any OTHER time.* call
    in the same function too -- a default cannot smuggle a second read.
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


THE_SCHEDULER_PACKAGE_READS_ONE_INJECTED_CLOCK = Rule(
    name="the-scheduler-package-reads-one-injected-clock",
    find=_raw_time_reads,
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
