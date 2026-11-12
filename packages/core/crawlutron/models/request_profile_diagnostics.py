"""The ``diagnostics["request_profile"]`` frame: the profile the engine resolved."""

from typing import TypedDict


class RequestProfileDiagnostics(TypedDict):
    """The ``diagnostics["request_profile"]`` frame: the profile the engine resolved."""

    user_agent_family: str
    cookie_jar: str
