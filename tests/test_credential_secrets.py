"""Credential secret-field read-edge discipline."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from crawlutron.models.credential import Credential
from crawlutron.safety.url_redaction import redacted_marker


def test_credential_repr_does_not_leak_secrets():
    """A Credential's repr/str must not contain plaintext token or password."""
    bearer = Credential(kind="bearer", token="super-secret-token-123")
    basic = Credential(kind="basic", username="user", password="super-secret-pass-456")

    bearer_repr = repr(bearer)
    basic_repr = repr(basic)

    assert "super-secret-token-123" not in bearer_repr, f"token leaked in repr: {bearer_repr}"
    assert "super-secret-pass-456" not in basic_repr, f"password leaked in repr: {basic_repr}"

    # str() also must not leak
    assert "super-secret-token-123" not in str(bearer)
    assert "super-secret-pass-456" not in str(basic)

    # But the secret value must still be accessible via .get_secret_value()
    assert bearer.token is not None and basic.password is not None
    assert bearer.token.get_secret_value() == "super-secret-token-123"
    assert basic.password.get_secret_value() == "super-secret-pass-456"


def test_the_redaction_marker_is_refused_at_the_read_edge():
    """A stored row holds the redaction marker, never a secret; a marker value
    presented as a live credential must fail typed at the read edge, never
    crawl as ``Authorization: Bearer [REDACTED]``."""
    for field in ("token", "username", "password"):
        value = "bearer" if field == "token" else "basic"
        payload = {"kind": value, field: redacted_marker()}
        if field == "username":
            payload = {"kind": "basic", "username": redacted_marker(), "password": "real-pass"}
        if field == "password":
            payload = {"kind": "basic", "username": "real-user", "password": redacted_marker()}

        with pytest.raises(ValidationError, match="redacted marker"):
            Credential.model_validate(payload)


def test_a_marker_inside_a_broader_value_is_also_refused():
    with pytest.raises(ValidationError, match="redacted marker"):
        Credential.model_validate({"kind": "bearer", "token": f"prefix {redacted_marker()}"})
