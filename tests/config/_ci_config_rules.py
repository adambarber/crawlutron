"""Pre-commit and prepare-action rules over the typed CI config models."""

from __future__ import annotations

import shlex
from collections.abc import Iterator

from scripts.jscpd_gate import configured_floor
from tests.config._bundle import STRICT_STAGES, Bundle, _finding
from tests.config._policy import Finding, RuleId


def precommit_hook_surface(bundle: Bundle) -> Iterator[Finding]:
    """Every hook CI invokes is declared, and stages stay inside the three surfaces."""
    declared = {hook.id for hook in bundle.precommit.hooks}
    invoked = set()
    for item in bundle.workflows:
        if item.stem != "ci":
            continue
        for job in item.jobs.values():
            for step in job.steps:
                tokens = shlex.split(str(step.run or ""))
                for index, token in enumerate(tokens[:-1]):
                    if token.endswith("pre-commit") and tokens[index + 1] == "run":
                        if index + 2 < len(tokens):
                            invoked.add(tokens[index + 2])
    for hook_id in sorted(invoked - declared):
        yield _finding(
            RuleId.PRECOMMIT_HOOK_SURFACE,
            bundle.precommit.path.name,
            1,
            f"ci runs hook {hook_id!r} that the config does not declare",
        )
    for hook in bundle.precommit.hooks:
        if not set(hook.stages) <= STRICT_STAGES:
            yield _finding(
                RuleId.PRECOMMIT_HOOK_SURFACE,
                bundle.precommit.path.name,
                hook.line,
                f"hook {hook.id!r} declares unknown stages {sorted(hook.stages)}",
            )


def jscpd_single_gate(bundle: Bundle) -> Iterator[Finding]:
    """One whole-repo jscpd gate at the configured floor, never a per-tree split."""
    jscpd = [hook for hook in bundle.precommit.hooks if hook.id.split("-")[0] == "jscpd"]
    if len(jscpd) != 1 or jscpd[0].id != "jscpd":
        yield _finding(
            RuleId.JSCPD_SINGLE_GATE,
            bundle.precommit.path.name,
            1,
            f"exactly one jscpd gate named 'jscpd' required, found {[h.id for h in jscpd]}",
        )
        return
    tokens = shlex.split(jscpd[0].entry)
    if "-m" not in tokens or "scripts.jscpd_gate" not in tokens or "all" not in tokens:
        yield _finding(
            RuleId.JSCPD_SINGLE_GATE,
            bundle.precommit.path.name,
            jscpd[0].line,
            "the jscpd gate must make one whole-repo invocation",
        )
    expected = configured_floor()
    flags = {}
    for index, token in enumerate(tokens[:-1]):
        if token.startswith("--min-") or token == "--threshold":
            flags[token[2:].replace("-", "_")] = tokens[index + 1]
    try:
        observed: tuple[int, int, float] | None = (
            int(flags["min_lines"]),
            int(flags["min_tokens"]),
            float(flags["threshold"]),
        )
    except (KeyError, TypeError, ValueError):
        observed = None
    if observed != expected:
        yield _finding(
            RuleId.JSCPD_SINGLE_GATE,
            bundle.precommit.path.name,
            jscpd[0].line,
            f"jscpd entry floor {observed} != configured floor {expected}",
        )


def coverage_at_prepush(bundle: Bundle) -> Iterator[Finding]:
    """Coverage enforces its floor at pre-push, never at commit time (#1358)."""
    coverage = [hook for hook in bundle.precommit.hooks if hook.id == "coverage"]
    if len(coverage) != 1:
        yield _finding(
            RuleId.COVERAGE_AT_PREPUSH, bundle.precommit.path.name, 1, "exactly one coverage hook required"
        )
        return
    if coverage[0].stages != ("pre-push",):
        yield _finding(
            RuleId.COVERAGE_AT_PREPUSH,
            bundle.precommit.path.name,
            coverage[0].line,
            f"coverage stages {list(coverage[0].stages)} must be ['pre-push']",
        )
    if not any(token.startswith("--cov-fail-under=") for token in coverage[0].entry.split()):
        yield _finding(
            RuleId.COVERAGE_AT_PREPUSH,
            bundle.precommit.path.name,
            coverage[0].line,
            "the pre-push coverage hook lost its --cov-fail-under floor",
        )


def prepare_action_env(bundle: Bundle) -> Iterator[Finding]:
    """The one prepare action pins 3.14, uv, and non-racing caches."""
    action = bundle.prepare
    versions = [step.inputs.get("python-version") for step in action.steps if "python-version" in step.inputs]
    if versions != ["3.14"]:
        yield _finding(
            RuleId.PREPARE_ACTION_ENV,
            action.path.name,
            1,
            f"prepare action python-version must be 3.14, got {versions}",
        )
    runs = " ".join(str(step.run or "") for step in action.steps)
    if "uv sync --frozen --extra dev" not in runs:
        yield _finding(
            RuleId.PREPARE_ACTION_ENV, action.path.name, 1, "prepare action must install the frozen dev lock"
        )
    if "pip install" in runs:
        yield _finding(RuleId.PREPARE_ACTION_ENV, action.path.name, 1, "prepare action must not use pip")
    if any("actions/checkout" in str(step.uses or "") for step in action.steps):
        yield _finding(
            RuleId.PREPARE_ACTION_ENV,
            action.path.name,
            1,
            "checkout belongs in the job, not the local action",
        )
    cache = next((step for step in action.steps if "actions/cache@" in str(step.uses or "")), None)
    paths = cache.inputs.get("path", "") if cache else ""
    for name in (".mypy_cache", ".ruff_cache"):
        if name not in paths:
            yield _finding(
                RuleId.PREPARE_ACTION_ENV, action.path.name, 1, f"prepare action must cache {name}"
            )
    if ".gate_fanout" in paths:
        yield _finding(
            RuleId.PREPARE_ACTION_ENV,
            action.path.name,
            1,
            "per-run gate pidfiles must not ride the shared cache",
        )
