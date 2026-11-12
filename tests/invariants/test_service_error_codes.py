"""Service raise sites and SDK error classes must agree on the wire code and status.

The SDK pins in ``tests/test_sdk_errors.py`` compare class constants against string
literals, so a service wire-code change leaves them green; this invariant reads the
LIVE service refusal at its raise site and asserts equality against the SDK class.
"""

from __future__ import annotations

import ast
import pathlib
import re

import crawlutron_sdk
from crawlutron.service.errors import DomainError, JobStateConflict

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

#: Live raise-site sources the wire triplets are re-derived from each run.
_RAISE_SITE_SOURCES = (
    "crawlutron/api/crawl_endpoint.py",
    "crawlutron/service/errors.py",
)

_DISCONNECT_SOURCE = "crawlutron/api/crawl_endpoint.py"


def _module_sources() -> dict[str, str]:
    return {name: (REPO_ROOT / name).read_text() for name in _RAISE_SITE_SOURCES}


def _wire_triplets() -> list[tuple[str, int, str]]:
    """Every (code, status, source) triplet literally raised in the sources."""
    triplets: list[tuple[str, int, str]] = []
    for source_name, text in _module_sources().items():
        for match in re.finditer(r'(?:http_error|super\(\).__init__)\((\d+), "([a-z_]+)"', text):
            triplets.append((match.group(2), int(match.group(1)), source_name))
    return triplets


def _sdk_classes_for(code: str) -> list[str]:
    return [
        name
        for name in crawlutron_sdk.errors.__all__
        if name.endswith("Error") and getattr(crawlutron_sdk, name).code == code
    ]


def _assert_pair(refusal: DomainError, sdk_class: type[crawlutron_sdk.CrawlutronError], origin: str) -> None:
    assert refusal.code == sdk_class.code, (
        f"{origin}: the service raises code {refusal.code!r} (status {refusal.status}) but the SDK "
        f"class {sdk_class.__name__} carries {sdk_class.code!r} -- the SDK constant drifted from "
        f"the live service raise site; update crawlutron_sdk/errors.py in the same change."
    )
    assert refusal.status == sdk_class.status_code, (
        f"{origin}: the service answers status {refusal.status} for code {refusal.code!r} but the "
        f"SDK class {sdk_class.__name__} declares status_code={sdk_class.status_code}."
    )


def test_conflict_refusal_matches_the_sdk_conflict_class():
    """JobStateConflict IS the 409/invalid_state service refusal the SDK mirrors."""
    _assert_pair(JobStateConflict("stale"), crawlutron_sdk.CrawlutronConflictError, "JobStateConflict")


def test_client_disconnected_refusal_matches_the_sdk_class():
    """Re-derive the 499 triplet from the raise-site AST, then pin it against the SDK."""
    tree = ast.parse(_module_sources()[_DISCONNECT_SOURCE])
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "http_error"
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == 499
    ]
    assert len(calls) == 1, (
        f"expected exactly one http_error(499, ...) raise site in {_DISCONNECT_SOURCE}, found "
        f"{len(calls)} -- the disconnect refusal moved; re-derive this pin from the new site."
    )
    code = calls[0].args[1]
    assert isinstance(code, ast.Constant), f"499 raise site code must be a string literal, got {code}"
    sdk_class = crawlutron_sdk.CrawlutronClientClosedRequestError
    assert code.value == sdk_class.code, (
        f"{_DISCONNECT_SOURCE} raises 499 with code {code.value!r} but {sdk_class.__name__} carries "
        f"{sdk_class.code!r} -- the SDK constant drifted from the live service raise site."
    )
    assert sdk_class.status_code == 499


def test_wire_codes_shared_with_the_sdk_agree_on_status():
    """A service triplet whose code an SDK class also carries: exactly one class,
    and the statuses must agree (a service-side status change reds here)."""
    for code, status, source in _wire_triplets():
        classes = _sdk_classes_for(code)
        if not classes:
            continue
        assert len(classes) == 1, (
            f"{source} raises wire code {code!r} at status {status}; expected exactly one SDK "
            f"class carrying it, found {classes}"
        )
        sdk_class = getattr(crawlutron_sdk, classes[0])
        assert sdk_class.status_code == status, (
            f"{source} answers status {status} for wire code {code!r} but the SDK class "
            f"{classes[0]} declares status_code={sdk_class.status_code}."
        )
