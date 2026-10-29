"""The ``request_profile`` record: one home for its closed key set."""

from typing import TypedDict

from crawlutron.models.json_value import JsonValue


class RequestProfileRecord(TypedDict, total=False):
    """Every key ``REQUEST_PROFILE_KEYS`` admits, at the value types readers accept.

    ``cookies`` and the pools stay ``JsonValue``: the vocabulary bounds them.
    """

    user_agent_family: str
    user_agents: list[JsonValue]
    accept_languages: list[JsonValue]
    fingerprint_seed: str
    fingerprint_rotation: bool
    cookie_jar: str
    cookie_namespace: str
    cookies: dict[str, JsonValue]
