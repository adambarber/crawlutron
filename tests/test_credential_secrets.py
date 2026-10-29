"""Red test for #504: Credential secret fields must not leak in repr/str."""

from __future__ import annotations

from crawlutron.models.credential import Credential


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
    assert bearer.token.get_secret_value() == "super-secret-token-123"
    assert basic.password.get_secret_value() == "super-secret-pass-456"
