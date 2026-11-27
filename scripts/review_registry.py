"""Typed policy registry for automated pull-request reviews."""

from __future__ import annotations

import fnmatch
import tomllib
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from scripts import review_support as _support
else:
    if __package__:
        from scripts import review_support as _support
    else:
        import review_support as _support

from tests._support._review_contract import (
    AgentDefinition,
    ProbeDefinition,
    ReviewRegistry,
    RoutingRule,
)

__all__ = [
    "AgentDefinition",
    "ProbeDefinition",
    "ReviewRegistry",
    "RoutingRule",
    "load_registry",
    "select_agent",
]

SEVERITIES: Final[frozenset[str]] = frozenset({"P0", "P1", "P2", "P3"})
PROBE_COMMANDS: Final[frozenset[str]] = frozenset({"structural-diff", "ci-gates", "invariants"})
AGENT_FIELDS: Final[frozenset[str]] = frozenset({"model", "prompt", "probes", "severity_threshold"})
PROBE_FIELDS: Final[frozenset[str]] = frozenset({"command", "description"})


def _strings(value: object, label: str) -> tuple[str, ...]:
    """Require a sequence of non-empty strings."""
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    return tuple(_support.non_empty_string(item, label) for item in value)


def _reject_unknown(data: dict[str, object], allowed: frozenset[str], label: str) -> None:
    """Refuse registry keys the schema does not define."""
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ValueError(f"{label} has unknown fields: {unknown}")


def _validate_prompt_path(prompt: str) -> str:
    """Require a repository-relative prompt under .github."""
    path = PurePosixPath(prompt)
    if path.is_absolute() or not path.parts or path.parts[0] != ".github" or ".." in path.parts:
        raise ValueError(f"agent prompt must be under .github: {prompt!r}")
    return prompt


def _load_agent(name: str, value: object, probes: dict[str, ProbeDefinition]) -> AgentDefinition:
    """Parse one agent definition."""
    data = _support.as_mapping(value, f"agent {name}")
    _reject_unknown(data, AGENT_FIELDS, f"agent {name}")
    severity = _support.non_empty_string(data.get("severity_threshold"), f"agent {name} severity_threshold")
    if severity not in SEVERITIES:
        raise ValueError(f"agent {name} has unknown severity threshold {severity!r}")
    probe_names = _strings(data.get("probes"), f"agent {name} probes")
    unknown = sorted(set(probe_names) - set(probes))
    if unknown:
        raise ValueError(f"agent {name} references unregistered probes: {unknown}")
    if len(set(probe_names)) != len(probe_names):
        raise ValueError(f"agent {name} repeats a probe")
    return AgentDefinition(
        name=name,
        model=_support.non_empty_string(data.get("model"), f"agent {name} model"),
        prompt=_validate_prompt_path(_support.non_empty_string(data.get("prompt"), f"agent {name} prompt")),
        probes=probe_names,
        severity_threshold=severity,
    )


def _load_probes(value: object) -> dict[str, ProbeDefinition]:
    """Parse the registered probe allowlist."""
    data = _support.as_mapping(value, "probes")
    probes: dict[str, ProbeDefinition] = {}
    for name, raw_probe in data.items():
        probe = _support.as_mapping(raw_probe, f"probe {name}")
        _reject_unknown(probe, PROBE_FIELDS, f"probe {name}")
        command = _support.non_empty_string(probe.get("command"), f"probe {name} command")
        if command not in PROBE_COMMANDS:
            raise ValueError(f"probe {name} has unknown command {command!r}")
        probes[name] = ProbeDefinition(
            name=name,
            command=command,
            description=_support.non_empty_string(probe.get("description"), f"probe {name} description"),
        )
    return probes


def _load_rules(
    value: object,
    label: str,
    agents: dict[str, AgentDefinition],
) -> tuple[RoutingRule, ...]:
    """Parse one routing-rule list."""
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"routing {label} must be a list")
    selector_key = "pattern" if label == "path_rules" else "label"
    rules: list[RoutingRule] = []
    for raw_rule in value:
        rule = _support.as_mapping(raw_rule, f"routing {label} rule")
        _reject_unknown(rule, frozenset({selector_key, "agent"}), f"routing {label} rule")
        selector = _support.non_empty_string(rule.get(selector_key), f"routing {label} {selector_key}")
        agent_name = _support.non_empty_string(rule.get("agent"), f"routing {label} agent")
        if agent_name not in agents:
            raise ValueError(f"routing {label} selects unregistered agent {agent_name!r}")
        rules.append(RoutingRule(selector=selector, agent=agent_name))
    return tuple(rules)


def load_registry(registry_path: Path) -> ReviewRegistry:
    """Load and validate the review policy registry."""
    raw = tomllib.loads(registry_path.read_text(encoding="utf-8"))
    root = _support.as_mapping(raw, "registry")
    _reject_unknown(root, frozenset({"defaults", "agents", "probes", "routing"}), "registry")
    probes = _load_probes(root.get("probes", {}))
    agents_data = _support.as_mapping(root.get("agents"), "agents")
    agents = {name: _load_agent(name, value, probes) for name, value in agents_data.items()}
    defaults = _support.as_mapping(root.get("defaults"), "defaults")
    default_agent = _support.non_empty_string(defaults.get("agent"), "default agent")
    if default_agent not in agents:
        raise ValueError(f"default agent {default_agent!r} is not registered")
    routing = _support.as_mapping(root.get("routing", {}), "routing")
    return ReviewRegistry(
        default_agent=default_agent,
        agents=agents,
        probes=probes,
        path_rules=_load_rules(routing.get("path_rules"), "path_rules", agents),
        label_rules=_load_rules(routing.get("label_rules"), "label_rules", agents),
    )


def select_agent(
    registry: ReviewRegistry,
    changed_files: tuple[str, ...] = (),
    labels: tuple[str, ...] = (),
) -> AgentDefinition:
    """Select an agent using path rules, then label rules, then the default."""
    for rule in registry.path_rules:
        if any(fnmatch.fnmatchcase(path, rule.selector) for path in changed_files):
            return registry.agents[rule.agent]
    for rule in registry.label_rules:
        if rule.selector in labels:
            return registry.agents[rule.agent]
    return registry.agents[registry.default_agent]
