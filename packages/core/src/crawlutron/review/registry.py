"""Type definitions for the review registry."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AgentDefinition:
    """One review agent's model, prompt, probes, and severity threshold."""

    name: str
    model: str
    prompt: str
    probes: tuple[str, ...]
    severity_threshold: str


@dataclass(frozen=True, slots=True)
class ProbeDefinition:
    """One allowlisted probe command and its description."""

    name: str
    command: str
    description: str


@dataclass(frozen=True, slots=True)
class RoutingRule:
    """A path or label rule that selects a registered agent."""

    selector: str
    agent: str


@dataclass(frozen=True, slots=True)
class ReviewRegistry:
    """The complete review policy loaded from one TOML registry."""

    default_agent: str
    agents: dict[str, AgentDefinition]
    probes: dict[str, ProbeDefinition]
    path_rules: tuple[RoutingRule, ...]
    label_rules: tuple[RoutingRule, ...]
