"""The scheduler package reads ONE clock: the injected one (#580).

#580: cooldowns stamped wall time while breakers ran monotonic -- three clock
families in one state object, and a partially dead ``clock=`` knob. Three
rules foreclose the category (one per sweep, so each bite's message names its
own walk): no clock read anywhere in the ``scheduler*.py`` modules except the
sanctioned constructor clock defaults. The law covers the ``time`` module
family BY NAME (any member, any alias); ``datetime`` is a second family the
coverage test records but does not sweep (#580 cycle 3).
"""

from __future__ import annotations

import ast
import pathlib
from collections.abc import Iterator

from tests.chokepoint_walks import REPO_ROOT, functions, offender_at
from tests.invariants._proof_paths import ELSEWHERE as _ELSEWHERE
from tests.rules import Bites, Registry, Rule, Silent

# Derived from the tree, not a hardcoded tuple: a scheduler sibling module is
# governed the moment it exists (adversarial review of #580, hole 4).
_TRANSPORT = REPO_ROOT / "crawlutron" / "transport"
_SCHEDULER_PACKAGE = tuple(_TRANSPORT.glob("scheduler*.py"))

_SCHED = REPO_ROOT / "crawlutron" / "transport" / "scheduler.py"
_SCHED_STATE = REPO_ROOT / "crawlutron" / "transport" / "scheduler_domain_state.py"


# The wall consumers DELEGATED by doctrine (#580 inventory): justified reads,
# each keeping its own clock. A new entry needs its justification in-commit.
_DELEGATED_WALL_CONSUMERS = frozenset(
    {
        "browser.py",
        "browser_driver.py",
        "browser_pool.py",
        "browser_render_phases.py",
        "cookie_jar.py",
        "cffi_transport.py",  # elapsed_ms timing for fetch telemetry (#685)
        "httpx_transport.py",  # elapsed_ms timing for fetch telemetry (#685)
        "fetch_policy.py",
        "fetchers.py",
        "freshness.py",
        "http.py",
        "http_cache.py",
        "redirect_walker.py",  # started= default for elapsed_ms (#685)
        "transport.py",  # http_413 elapsed_ms (#685)
    }
)

# The time module's CLOCK members -- the names whose bare call reads a clock;
# a from-import of a NON-clock member is not a timeline read (cycle 3, 3c).
_TIME_CLOCK_MEMBERS = frozenset(
    {
        "time",
        "time_ns",
        "monotonic",
        "monotonic_ns",
        "perf_counter",
        "perf_counter_ns",
        "process_time",
        "process_time_ns",
        "thread_time",
        "thread_time_ns",
        "clock_gettime",
        "clock_gettime_ns",
        "clock_settime",
        "clock_settime_ns",
    }
)


def _mentions_time_module(tree: ast.Module) -> bool:
    """Whether the module's CODE binds or reads a clock family: an import of
    ``time`` or ``datetime``, or any Name rooted at one. The one-timeline law
    sweeps the time family by name; datetime is recorded as a boundary (a
    datetime pressure clock in scheduler* would need its own sweep)."""
    return any(
        (
            isinstance(node, ast.Import)
            and any(
                a.name == "time"
                or a.name.startswith("time.")
                or a.name == "datetime"
                or a.name.startswith("datetime.")
                for a in node.names
            )
        )
        or (isinstance(node, ast.ImportFrom) and node.module in {"time", "datetime"})
        or (isinstance(node, ast.Name) and node.id in {"time", "datetime"} and isinstance(node.ctx, ast.Load))
        for node in ast.walk(tree)
    )


def test_the_governing_glob_covers_every_transport_module_mentioning_time():
    """Coverage arming (adversarial pass 2, hole 4; rglob pass 3): a module that
    escapes the ``scheduler*`` set -- renamed, or in a NESTED package -- while
    touching a clock family fails here; the rule's reach is proved, not
    assumed. The doctrine delegates justified wall reads (cookie-jar RFC 6265
    epochs, HTTP-date instants, HMAC windows, persisted stamps) to named
    consumers; the one-timeline law governs the DOMAIN PRESSURE state (the
    scheduler*.py modules), never those."""
    governed = {p.name for p in _SCHEDULER_PACKAGE} | _DELEGATED_WALL_CONSUMERS
    escapees = [
        p.relative_to(REPO_ROOT)
        for p in sorted(_TRANSPORT.rglob("*.py"))
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


def _is_default_expression(fn: ast.FunctionDef | ast.AsyncFunctionDef, node: ast.AST) -> bool:
    """Whether ``node`` IS one of ``fn``'s parameter defaults (identity, not
    line-span: a line-range exemption would bless every call in the body)."""
    return any(node is default for default in (fn.args.kw_defaults or []) + list(fn.args.defaults))


def _offending_time_call(dotted: str | None, aliased_roots: set[str]) -> bool:
    """Whether a dotted callee reads a clock under the one-timeline law: any
    attribute chain rooted at the time module (or an alias bound to it)."""
    if dotted is None:
        return False
    root = dotted.split(".")[0]
    return root == "time" or root in aliased_roots


def _aliased_time_roots(tree: ast.Module) -> set[str]:
    """Local names bound to the time module, incl. submodules
    (``import time as t``, ``import time.machine as tm``)."""
    return {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name == "time" or alias.name.startswith("time.")
    }


def _from_imported_clocks(tree: ast.Module) -> set[str]:
    """Local names bound to a CLOCK member via ``from time import ...`` at any
    scope (cycle 2: module-level-only missed function-local imports)."""
    from tests.invariants._clock_family import from_time_imports

    return from_time_imports(tree, _TIME_CLOCK_MEMBERS)


def _clock_call_offenders(tree: ast.Module, path: pathlib.Path) -> Iterator[str]:
    """Every ``time.<member>(...)`` (or aliased-root) call in the WHOLE module
    AST (cycle 2: module-level calls outside any def were ungoverned); the
    sanctioned clock defaults are exempt -- the default EXPRESSION only, by
    identity, never the enclosing body (cycle 3: a line-span exemption blessed
    a raw mono call inside TokenBucket.__init__'s body)."""
    if path not in _SCHEDULER_PACKAGE:
        return
    aliased_roots = _aliased_time_roots(tree)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        dotted = _dotted(node.func)
        if not _offending_time_call(dotted, aliased_roots):
            continue
        exempt = dotted == "time.monotonic" and any(
            _is_default_expression(fn, node) for fn in functions(tree) if _has_clock_default(fn)
        )
        if not exempt:
            yield offender_at(
                path,
                node,
                f"{dotted} reads the clock directly: every timestamp in the scheduler "
                f"flows through the injected clock (#580)",
            )


def _from_import_offenders(tree: ast.Module, path: pathlib.Path) -> Iterator[str]:
    """Bare-name calls of from-imported clock members, at ANY scope (cycle 2:
    ``from time import time; time()`` has no attribute node for the first
    sweep to see)."""
    if path not in _SCHEDULER_PACKAGE:
        return
    imported = _from_imported_clocks(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in imported:
            yield offender_at(
                path,
                node,
                f"{node.func.id}() reads the clock via a from-import: spell it time.* so "
                f"the one-clock detector sees it, or inject the clock (#580)",
            )


def _wall_default_offenders(tree: ast.Module, path: pathlib.Path) -> Iterator[str]:
    """Clock defaults of the wrong family: a wall default is the original #580
    defect in two tokens, invisible to any call-site sweep. Covers both
    spellings -- the dotted ``time.time`` and the from-imported alias used as
    the default (``from time import time as wall; def f(clock=wall)``), which
    hid from BOTH sweeps (cycle 4, hole 2)."""
    if path not in _SCHEDULER_PACKAGE:
        return
    imported = _from_imported_clocks(tree)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for default in (node.args.kw_defaults or []) + list(node.args.defaults):
            dotted = _dotted(default)
            wrong_dotted = dotted is not None and dotted.startswith("time.") and dotted != "time.monotonic"
            aliased = isinstance(default, ast.Name) and default.id in imported
            if wrong_dotted or aliased:
                assert isinstance(default, ast.expr)
                yield offender_at(
                    path,
                    default,
                    f"clock default is {dotted or getattr(default, 'id', '?')}: the scheduler's one timeline "
                    f"is monotonic; a wall default is the #580 defect",
                )


THE_SCHEDULER_PACKAGE_CALLS_NO_CLOCK_DIRECTLY = Rule(
    name="the-scheduler-package-calls-no-clock-directly",
    find=_clock_call_offenders,
    summary=(
        "no direct time.* call in the scheduler modules outside the sanctioned "
        "clock defaults: the injected monotonic clock is the package's single "
        "timeline, and a second family is the #580 defect"
    ),
    bite=lambda: (
        Bites(
            "import time\n\nclass DomainScheduler:\n    def run_one(self, job):\n        now = time.time()\n",
            "time.time reads the clock directly",
            at=_SCHED,
        ),
        Bites(
            "import time\n\n"
            "def breaker_remaining(self, *, now):\n"
            "    return max(0.0, self.circuit_open_until - time.monotonic())\n",
            "time.monotonic reads the clock directly",
            at=_SCHED_STATE,
        ),
        # Aliased module root: ``import time as t`` reads via t.time, invisible
        # to a root-name check (second adversarial pass of #580).
        Bites(
            "import time as t\n\ndef probe(self):\n    return t.time()\n",
            "t.time reads the clock directly",
            at=_SCHED,
        ),
        # Module-level clock call outside any def: the def-walk missed it.
        Bites(
            "import time\n\nSTARTED_AT = time.monotonic()\n",
            "time.monotonic reads the clock directly",
            at=_SCHED,
        ),
        # A raw mono call in a sanctioned-default function's BODY must fire:
        # the exemption is the default EXPRESSION, never the body (cycle 3).
        Bites(
            "import time\n\n"
            "class TokenBucket:\n"
            "    def __init__(self, *, clock=time.monotonic):\n"
            "        self._clock = clock\n"
            "        self.last_refill = time.monotonic()\n",
            "time.monotonic reads the clock directly",
            at=_SCHED,
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

THE_SCHEDULER_PACKAGE_FROM_IMPORTS_NO_CLOCK = Rule(
    name="the-scheduler-package-from-imports-no-clock",
    find=_from_import_offenders,
    summary=(
        "no bare-name clock call via from time import in the scheduler modules: "
        "a from-import hides the read from the attribute sweep (#580)"
    ),
    bite=lambda: (
        Bites(
            "from time import monotonic\n\ndef remaining(self):\n    return monotonic()\n",
            "via a from-import",
            at=_SCHED_STATE,
        ),
        # The ALIASED shape binds under asname (second pass of #580).
        Bites(
            "from time import monotonic as mono\n\ndef remaining(self):\n    return mono()\n",
            "via a from-import",
            at=_SCHED_STATE,
        ),
        # A FUNCTION-LOCAL from-import is in scope, not tree.body only (pass 2).
        Bites(
            "def remaining(self):\n    from time import time as wall\n    return wall()\n",
            "via a from-import",
            at=_SCHED_STATE,
        ),
        # The ns twins and the clock_gettime family are clock reads too (pass 4).
        Bites(
            "from time import clock_gettime, CLOCK_MONOTONIC\n\n"
            "def remaining(self):\n    return clock_gettime(CLOCK_MONOTONIC)\n",
            "via a from-import",
            at=_SCHED_STATE,
        ),
    ),
    clean=lambda: (
        # A non-clock member is not a timeline read (cycle 3, precision 3c).
        Silent("from time import machine\n\ndef f():\n    return machine()\n", at=_SCHED_STATE),
        # Path-keyed: the same read elsewhere must stay quiet.
        Silent("from time import monotonic\n\ndef remaining(self):\n    return monotonic()\n", at=_ELSEWHERE),
    ),
)

THE_SCHEDULER_PACKAGE_DEFAULTS_TO_MONOTONIC = Rule(
    name="the-scheduler-package-defaults-to-monotonic",
    find=_wall_default_offenders,
    summary=(
        "every clock default in the scheduler modules is time.monotonic: a wall "
        "default is the original #580 defect in two tokens"
    ),
    bite=lambda: (
        Bites(
            "import time\n\n"
            "class DomainScheduler:\n"
            "    def __init__(self, *, clock=time.time):\n"
            "        self._clock = clock\n",
            "clock default is time.time",
            at=_SCHED,
        ),
        # The from-imported alias used AS the default hides from both prior
        # sweeps -- dotted-only defaults and bare-name CALL checks (pass 4).
        Bites(
            "from time import time as wall\n\n"
            "class DomainScheduler:\n"
            "    def __init__(self, *, clock=wall):\n"
            "        self._clock = clock\n",
            "clock default is wall",
            at=_SCHED,
        ),
    ),
    clean=lambda: (
        # The sanctioned default is quiet; and the wrong-family default placed
        # outside the governed paths must stay quiet (path-keyed proof).
        Silent(
            "import time\n\n"
            "class DomainScheduler:\n"
            "    def __init__(self, *, clock=time.monotonic):\n"
            "        self._clock = clock\n",
            at=_SCHED,
        ),
        Silent(
            "import time\n\n"
            "class DomainScheduler:\n"
            "    def __init__(self, *, clock=time.time):\n"
            "        self._clock = clock\n",
            at=_ELSEWHERE,
        ),
    ),
)

RULES = Registry(
    (
        THE_SCHEDULER_PACKAGE_CALLS_NO_CLOCK_DIRECTLY,
        THE_SCHEDULER_PACKAGE_FROM_IMPORTS_NO_CLOCK,
        THE_SCHEDULER_PACKAGE_DEFAULTS_TO_MONOTONIC,
    )
)


def test_no_rule_violations():
    """The scheduler modules read only the injected clock (#580)."""
    RULES.check_all_clean()
