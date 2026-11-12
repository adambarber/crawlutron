"""The auth block of the capabilities payload: how a caller identifies."""

from typing import TypedDict


class AuthCapabilitiesRecord(TypedDict):
    """The ``capabilities["auth"]`` block, spelled where the endpoint writes it."""

    caller_credential: str
    anonymous_caller: str
    key_management: str
