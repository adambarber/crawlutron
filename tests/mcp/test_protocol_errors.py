"""ProtocolValidationError carries a usable message for every payload it accepts."""

from __future__ import annotations

from app.mcp.protocol import ProtocolValidationError


def test_non_dict_payload_does_not_crash():
    # #446: ``payload`` is JsonValue, so a non-dict must produce the default
    # message, not an UnboundLocalError from reading ``message`` unassigned.
    error = ProtocolValidationError(400, "missing _meta")
    assert error.status_code == 400
    assert error.payload == "missing _meta"
    assert str(error) == "validation error"


def test_dict_payload_message_wins():
    error = ProtocolValidationError(400, {"error": {"message": "bad version"}})
    assert str(error) == "bad version"
    assert error.payload == {"error": {"message": "bad version"}}


def test_none_message_falls_back_to_default():
    # #493: a present-but-None message read as str(None); JsonValue admits None,
    # so only a non-empty string may carry the payload's message.
    error = ProtocolValidationError(400, {"error": {"message": None}})
    assert str(error) == "validation error"
