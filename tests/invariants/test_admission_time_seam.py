"""The admission kernel uses no concrete time/sleep primitive: only injected seams.

The source-scan gate fails on any ``time.*`` clock call, ``asyncio.sleep``/``asyncio.Event``
(and aliases) in ``admission/**``; the Clock/Signal are injected, never implemented here.
"""

from __future__ import annotations

import ast
import pathlib

from tests.chokepoint_walks import ADMISSION_ROOT, APP_ROOT, offender_at, trees_under
from tests.invariants._clock_family import dotted_name
from tests.rules import Bites, Rule, Silent

_SYNTHETIC = ADMISSION_ROOT / "_synthetic.py"
_ELSEWHERE = APP_ROOT / "admission" / "policy.py"

#: The time module's CLOCK members: the names whose call reads a timeline.
_TIME_CLOCK_MEMBERS = frozenset(
    {
        "time",
        "time_ns",
        "monotonic",
        "monotonic_ns",
        "perf_counter",
        "perf_counter_ns",
        "process_time",
        "clock_gettime",
        "sleep",
    }
)


def _roots(tree: ast.Module) -> tuple[set[str], set[str]]:
    """Aliased roots for ``time`` and ``asyncio`` (``import time as t``, ``import asyncio as aio``)."""
    time_roots: set[str] = set()
    asyncio_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "time":
                    time_roots.add(alias.asname or "time")
                elif alias.name == "asyncio":
                    asyncio_roots.add(alias.asname or "asyncio")
        elif isinstance(node, ast.ImportFrom):
            if node.module == "time":
                for alias in node.names:
                    if alias.name in _TIME_CLOCK_MEMBERS:
                        time_roots.add(alias.asname or alias.name)
    return time_roots, asyncio_roots


def _offending_call(
    dotted: str | None,
    func: ast.expr,
    time_roots: set[str],
    asyncio_roots: set[str],
) -> str | None:
    """The primitive a call violates, or None if the call is sanctioned.

    ``asyncio.wait_for``/``asyncio.wait`` are the bounded-wait wrappers the kernel
    legitimately uses inside ``__aenter__``; ``asyncio.sleep``/``asyncio.Event`` are violations.
    """
    if isinstance(func, ast.Name):
        if func.id in time_roots:
            return f"{func.id} is a concrete time read: inject the Clock seam"
        return None
    if dotted is None:
        return None
    root = dotted.split(".")[0]
    if root in time_roots:
        return f"{dotted} is a concrete time read: inject the Clock seam"
    if root in asyncio_roots:
        leaf = dotted.rsplit(".", 1)[-1]
        if leaf == "sleep":
            return f"{dotted} is a concrete sleep: inject the Signal seam"
        if leaf == "Event":
            return f"{dotted} constructs an asynchronous event: inject the Signal seam"
    return None


def _time_seam_offenders(tree: ast.Module, path: pathlib.Path) -> list[str]:
    """Every concrete time read, sleep or event construction in an admission module.

    Path-keyed: the detector owns only ``admission/**``, so the URL-admission
    package's legitimate time reads stay quiet."""
    if ADMISSION_ROOT not in path.parents and path.parent != ADMISSION_ROOT:
        return []
    time_roots, asyncio_roots = _roots(tree)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            dotted = dotted_name(node.func)
            reason = _offending_call(dotted, node.func, time_roots, asyncio_roots)
            if reason is not None:
                offenders.append(offender_at(path, node, reason))
    return offenders


ADMISSION_USES_NO_CONCRETE_TIME_OR_SLEEP = Rule(
    name="admission-uses-no-concrete-time-or-sleep",
    find=_time_seam_offenders,
    summary=(
        "no admission/** module may read a clock or await a concrete event: "
        "the injected Clock/Signal are the package's only time surfaces (\u00a78)"
    ),
    scope=lambda *, exclude: trees_under(ADMISSION_ROOT),
    proof_path=_SYNTHETIC,
    bite=lambda: (
        Bites(
            "import time\n\nclass C:\n    def f(self):\n        return time.monotonic()\n",
            "is a concrete time read",
            at=_SYNTHETIC,
        ),
        Bites(
            "import asyncio\n\nasync def f(self):\n    await asyncio.sleep(1)\n",
            "is a concrete sleep",
            at=_SYNTHETIC,
        ),
        Bites(
            "from time import monotonic\n\ndef f(self):\n    return monotonic()\n",
            "is a concrete time read",
            at=_SYNTHETIC,
        ),
        Bites(
            "import asyncio as aio\n\nasync def f(self):\n    await aio.sleep(1)\n",
            "is a concrete sleep",
            at=_SYNTHETIC,
        ),
        Bites(
            "import asyncio\n\nasync def f(self):\n    ev = asyncio.Event()\n",
            "constructs an asynchronous event",
            at=_SYNTHETIC,
        ),
        Bites(
            "import time\n\nSTART = time.time()\n",
            "is a concrete time read",
            at=_SYNTHETIC,
        ),
    ),
    clean=lambda: (
        # The URL-admission package legitimately reads time: the path gate must
        # spare it (the exact collision the descriptor exists to prevent).
        Silent(
            "import time\n\nSTART = time.time()\n",
            at=APP_ROOT / "admission" / "_synthetic.py",
        ),
        # A kernel module that references only the injected names stays quiet.
        Silent(
            "class C:\n    def f(self):\n        return self._clock.now().value\n",
            at=_SYNTHETIC,
        ),
        Silent("X = 1\n", at=_SYNTHETIC),
        Silent("X = 1\n", at=_ELSEWHERE),
    ),
)

RULES = (ADMISSION_USES_NO_CONCRETE_TIME_OR_SLEEP,)


def test_admission_uses_no_concrete_time_or_sleep() -> None:
    """The kernel reads time only through its injected Clock/Signal (source-scan gate)."""
    for rule in RULES:
        rule.scan()
        rule.check_bites()
