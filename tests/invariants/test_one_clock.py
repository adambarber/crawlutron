"""The scheduler package reads ONE clock: the injected one (#580).

#580: cooldowns stamped wall time while breakers ran monotonic -- three clock
families in one state object, and a partially dead ``clock=`` knob. This rule
forecloses the category: no ``time.*`` call anywhere in the two scheduler
modules except the sanctioned constructor clock defaults.
"""

from __future__ import annotations

import ast
import pathlib

from tests.chokepoint_walks import REPO_ROOT, functions, offender_at
from tests.invariants._proof_paths import ELSEWHERE as _ELSEWHERE
from tests.rules import Bites, Registry, Rule, Silent

# Derived from the tree, not a hardcoded tuple: a scheduler sibling module is
# governed the moment it exists (adversarial review of #580, hole 4).
_TRANSPORT = REPO_ROOT / "crawlutron" / "transport"
_SCHEDULER_PACKAGE = tuple(_TRANSPORT.glob("scheduler*.py"))


# The wall consumers DELEGATED by doctrine (#580 inventory): justified reads,
# each keeping its own clock. A new entry needs its justification in-commit.
_DELEGATED_WALL_CONSUMERS = frozenset(
    {
        "browser.py",
        "browser_driver.py",
        "browser_pool.py",
        "browser_render_phases.py",
        "cookie_jar.py",
        "fetchers.py",
        "freshness.py",
        "http.py",
        "http_cache.py",
    }
)


def _mentions_time_module(tree: ast.Module) -> bool:
    """Whether the module's CODE binds or reads the ``time`` module: an import
    of it, or any Name/Attribute rooted at it. Comments don't count; the real
    test of "mentions" is whether the governing rule could ever see a read."""
    return any(
        (
            isinstance(node, ast.Import)
            and any(a.name == "time" or a.name.startswith("time.") for a in node.names)
        )
        or (isinstance(node, ast.ImportFrom) and node.module == "time")
        or (isinstance(node, ast.Name) and node.id == "time" and isinstance(node.ctx, ast.Load))
        for node in ast.walk(tree)
    )


def test_the_governing_glob_covers_every_transport_module_mentioning_time():
    """Coverage arming (adversarial pass 2, hole 4): a module that RENAMES out
    of the ``scheduler*`` glob while touching ``time`` fails here -- the rule's
    reach is proved, not assumed. The doctrine delegates justified wall reads
    (cookie-jar RFC 6265 epochs, HTTP-date instants, HMAC windows, persisted
    stamps) to named consumers; the one-timeline law governs the DOMAIN
    PRESSURE state (scheduler*), never those."""
    governed = {p.name for p in _SCHEDULER_PACKAGE} | _DELEGATED_WALL_CONSUMERS
    escapees = [
        p.relative_to(REPO_ROOT)
        for p in sorted(_TRANSPORT.glob("*.py"))
        if p.name not in governed and _mentions_time_module(ast.parse(p.read_text()))
    ]
    assert not escapees, (
        f"transport modules outside the governed set mention time: "
        f"{escapees} -- govern them (scheduler*.py) or declare them delegated "
        f"wall consumers with their justification, in the same commit (#580)"
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


def _within(fn: ast.FunctionDef | ast.AsyncFunctionDef, node: ast.AST) -> bool:
    """Whether ``node``'s source span sits inside ``fn``'s."""
    return fn.lineno <= node.lineno <= fn.end_lineno


def _offending_time_call(dotted: str | None, aliased_roots: set[str]) -> bool:
    """Whether a dotted callee reads the clock under the one-timeline law:
    the ``time`` module itself, or any alias bound to it (``import time as t``)."""
    if dotted is None:
        return False
    root = dotted.split(".")[0]
    return root == "time" or root in aliased_roots


def _all_time_offenders(tree: ast.Module, path: pathlib.Path) -> list[str]:
    """Every clock read the package's ONE-timeline law refuses (#580).

    Three sweeps in one detector so the accounting audit sees one wired walk,
    each over the WHOLE module AST (second adversarial pass of #580: module
    scope and function-local imports are governed too, not just top-level
    defs with top-level imports):
    - raw time.* attribute calls, including aliased module roots
      (``import time as t``) -- sanctioned clock defaults exempt, per function;
    - clock defaults of the wrong family;
    - bare-name calls via ``from time import ...`` at ANY scope.
    """
    if path not in _SCHEDULER_PACKAGE:
        return
    aliased_roots = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name == "time"
    }
    imported = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "time"
        for alias in node.names
        if alias.name != "*"
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            dotted = _dotted(node.func)
            if _offending_time_call(dotted, aliased_roots):
                exempt = dotted == "time.monotonic" and any(
                    _has_clock_default(fn) for fn in functions(tree) if _within(fn, node)
                )
                if not exempt:
                    yield offender_at(
                        path,
                        node,
                        f"{dotted} reads the clock directly: every timestamp in the scheduler "
                        f"flows through the injected clock (#580)",
                    )
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in imported:
            yield offender_at(
                path,
                node,
                f"{node.func.id}() reads the clock via a from-import: spell it time.* so "
                f"the one-clock detector sees it, or inject the clock (#580)",
            )
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for default in (node.args.kw_defaults or []) + list(node.args.defaults):
                dotted = _dotted(default)
                if dotted is not None and dotted.startswith("time.") and dotted != "time.monotonic":
                    yield offender_at(
                        path,
                        default,
                        f"clock default is {dotted}: the scheduler's one timeline is monotonic; "
                        f"a wall default is the #580 defect",
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
            "time.time reads the clock directly",
            at=REPO_ROOT / "crawlutron" / "transport" / "scheduler.py",
        ),
        Bites(
            "import time\n\n"
            "def breaker_remaining(self, *, now):\n"
            "    return max(0.0, self.circuit_open_until - time.monotonic())\n",
            "time.monotonic reads the clock directly",
            at=REPO_ROOT / "crawlutron" / "transport" / "scheduler_domain_state.py",
        ),
        # Aliased module root: ``import time as t`` reads via t.time, invisible
        # to a root-name check (second adversarial pass of #580).
        Bites(
            "import time as t\n\ndef probe(self):\n    return t.time()\n",
            "t.time reads the clock directly",
            at=REPO_ROOT / "crawlutron" / "transport" / "scheduler.py",
        ),
        # Module-level clock call outside any def: the def-walk missed it.
        Bites(
            "import time\n\nSTARTED_AT = time.monotonic()\n",
            "time.monotonic reads the clock directly",
            at=REPO_ROOT / "crawlutron" / "transport" / "scheduler.py",
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
        # ...and a FUNCTION-LOCAL from-import is in scope, not tree.body only
        # (second adversarial pass of #580).
        Bites(
            "def remaining(self):\n    from time import time as wall\n    return wall()\n",
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
