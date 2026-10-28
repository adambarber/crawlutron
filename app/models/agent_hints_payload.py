"""The raw agent-hints payload, before pydantic validation: its key set."""

from typing import TypedDict

from app.models.example_policy import ExamplePolicy


class AgentHintsPayload(TypedDict):
    """The raw agent-hints record, before pydantic validation."""

    service: str
    strategies: dict[str, str]
    extractions: dict[str, str]
    guidance: list[str]
    example_policies: dict[str, ExamplePolicy]
