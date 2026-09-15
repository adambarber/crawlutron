"""P0-2: one shared classification -> status resolver, not two that disagree.

``http._status_for_classification`` (module function) and
``BrowserRenderer._status_for_classification`` (method) were independent
if-chains over overlapping sets. They agreed on the labels they both knew and
disagreed on everything else: an unrecognized classification came back
``"failed"`` from the HTTP path and ``"blocked"`` from the browser path -- and
``not_modified`` (produced only by the HTTP path, consumed cross-module by the
engine) would have come back ``"blocked"`` from the browser copy. A new
classification label therefore silently got the opposite status depending on
which fetcher served the page.

The fix is one table in :mod:`app.constants` that both call sites delegate to.
"""

import pytest

from app import constants as app_constants
from app.classify import CLASSIFICATION_SIGNATURES, STRONG_CLASSIFICATION_MARKERS
from app.constants import (
    BROWSER_BLOCKED_CLASSIFICATIONS,
    FAILED_CLASSIFICATIONS,
    HTTP_BLOCKED_CLASSIFICATIONS,
)
from app.crawl.strategies import http as http_mod
from app.crawl.strategies.browser import BrowserRenderer

# Labels that are not in the taxonomy at all: this is exactly where the two
# historical implementations disagreed.
UNRECOGNIZED_LABELS = ("totally_new_label", "some_future_block", "")


def _classifiable_labels() -> list[str]:
    """Every label the classifiers can emit, plus the cross-module freshness one."""
    labels = {"rendered", "not_modified"}
    labels.update(signature[0] for signature in CLASSIFICATION_SIGNATURES)
    labels.update(marker[0] for marker in STRONG_CLASSIFICATION_MARKERS)
    return sorted(labels)


@pytest.fixture
def browser_resolver():
    renderer = BrowserRenderer()
    return renderer._status_for_classification


def test_http_and_browser_paths_report_the_same_status_for_every_label(browser_resolver):
    """The defect: two resolvers, one taxonomy, opposite answers for the tail.

    Status is part of the API contract (and of job stats), so it must not
    depend on which fetcher happened to serve the page.
    """
    labels = _classifiable_labels() + sorted(FAILED_CLASSIFICATIONS)
    labels += sorted(HTTP_BLOCKED_CLASSIFICATIONS | BROWSER_BLOCKED_CLASSIFICATIONS)
    labels += list(UNRECOGNIZED_LABELS)

    mismatches = {}
    for label in labels:
        http_status = http_mod._status_for_classification(label)
        browser_status = browser_resolver(label)
        if http_status != browser_status:
            mismatches[label] = (http_status, browser_status)
    assert not mismatches, f"http/browser status disagreement (http, browser): {mismatches}"


def test_unknown_classification_has_one_documented_default(browser_resolver):
    """A label nobody knows about must resolve identically on both paths."""
    for label in UNRECOGNIZED_LABELS:
        status = http_mod._status_for_classification(label)
        assert status == browser_resolver(label) == app_constants.UNKNOWN_CLASSIFICATION_STATUS
        # The safe default may never be "success": an unrecognized label has
        # not produced verified content.
        assert status != "success"


def test_every_classifiable_label_is_an_explicit_table_entry():
    """Drift guard: no classifier output may fall through to the default.

    Adding a signature to ``app.classify`` without giving it a status is the
    regression this pins; it is what let the two if-chains drift apart in the
    first place (an unlisted label silently took whichever default the local
    copy happened to use).
    """
    table = app_constants.CLASSIFICATION_STATUS_BY_LABEL
    missing = [label for label in _classifiable_labels() if label not in table]
    assert not missing, f"classifications with no explicit status entry: {missing}"


def test_resolver_is_a_table_not_a_per_module_if_chain(browser_resolver):
    """Both call sites must delegate to the one shared resolver."""
    for label in _classifiable_labels() + list(UNRECOGNIZED_LABELS):
        expected = app_constants.status_for_classification(label)
        assert http_mod._status_for_classification(label) == expected
        assert browser_resolver(label) == expected


def test_known_group_statuses_are_unchanged(browser_resolver):
    """Inverse case: the working paths must keep their exact statuses."""
    assert http_mod._status_for_classification("rendered") == "success"
    assert browser_resolver("rendered") == "success"
    # Conditional freshness is a success: the caller's validators proved it.
    assert http_mod._status_for_classification("not_modified") == "success"
    assert app_constants.status_for_classification("not_modified") == "success"
    for classification in BROWSER_BLOCKED_CLASSIFICATIONS:
        assert browser_resolver(classification) == "blocked"
    for classification in HTTP_BLOCKED_CLASSIFICATIONS:
        assert http_mod._status_for_classification(classification) == "blocked"
    for classification in FAILED_CLASSIFICATIONS:
        assert http_mod._status_for_classification(classification) == "failed"
        assert browser_resolver(classification) == "failed"
