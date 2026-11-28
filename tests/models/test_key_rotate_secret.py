"""The key-rotation secret is a typed, bounded credential at the model boundary."""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from crawlutron.models.key_rotate_request import KeyRotateRequest

_SECRET_ID = "a" * 16
_SECRET_BODY = "b" * 492  # "ck_" + 16 + "." + 492 == 512 characters
_VALID_SECRET = f"ck_{_SECRET_ID}.{_SECRET_BODY}"


def test_secret_repr_never_exposes_the_value():
    request = KeyRotateRequest(new_secret=_VALID_SECRET)
    assert _VALID_SECRET not in repr(request)
    assert _VALID_SECRET not in str(request.new_secret)


def test_secret_is_a_typed_secret_str():
    request = KeyRotateRequest(new_secret=_VALID_SECRET)
    assert isinstance(request.new_secret, SecretStr)
    assert request.new_secret.get_secret_value() == _VALID_SECRET


def test_overlong_secret_is_refused_at_construction():
    too_long = f"ck_{_SECRET_ID}." + "b" * 493  # 513 characters
    with pytest.raises(ValidationError):
        KeyRotateRequest(new_secret=too_long)


def test_empty_secret_is_refused():
    with pytest.raises(ValidationError):
        KeyRotateRequest(new_secret="")


def test_boundary_length_secret_is_accepted():
    request = KeyRotateRequest(new_secret=_VALID_SECRET)
    assert request.new_secret.get_secret_value() == _VALID_SECRET


def test_wrong_case_field_name_is_refused():
    with pytest.raises(ValidationError):
        KeyRotateRequest.model_validate({"NEW_SECRET": _VALID_SECRET})
