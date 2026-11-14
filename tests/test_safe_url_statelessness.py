"""safe_url construction is stateless: no module-level record is mutated on admit."""

import importlib

import pytest
import safe_url.normalized_url as nu
from safe_url.normalized_url import NormalizedUrl

from tests._policy_factories import UNRESTRICTED_HOST_POLICY

mve = importlib.import_module("safe_url.mapped_value_errors")


def _foreign(host: str) -> str:
    raise ValueError("foreign programming bug")


def test_mapped_value_errors_module_holds_no_record() -> None:
    """The seam is empty: no _RECORD and no tally survive the removal."""
    assert not hasattr(mve, "_RECORD")
    assert not hasattr(mve, "record_mapped_value_error")


def test_normalized_url_construction_is_stateless() -> None:
    """A foreign-ValueError remap raises invalid_url with no module-level mutation."""
    original = nu.idna_host
    nu.idna_host = _foreign
    try:
        with pytest.raises(ValueError, match="invalid_url"):
            NormalizedUrl("http://example.com/", policy=UNRESTRICTED_HOST_POLICY)
    finally:
        nu.idna_host = original
    assert not hasattr(mve, "_RECORD")
    assert not hasattr(mve, "record_mapped_value_error")
