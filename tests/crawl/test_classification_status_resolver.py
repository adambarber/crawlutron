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

The fix is one table in :mod:`app.constants`. Both fetchers now call it
directly, and the per-module resolvers are gone; the drift guard below asserts
they stay gone, because the two implementations agreeing was the whole defect.
"""

import inspect

import pytest

from app import constants as app_constants
from app.classify import CLASSIFICATION_SIGNATURES, STRONG_CLASSIFICATION_MARKERS
from app.constants import (
    BROWSER_BLOCKED_CLASSIFICATIONS,
    FAILED_CLASSIFICATIONS,
    HTTP_BLOCKED_CLASSIFICATIONS,
    status_for_classification,
)
from app.crawl.strategies import browser as browser_mod
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
    """The resolver the browser path actually uses.

    Both paths now share one function, so this is the same object the HTTP path
    calls -- which is the point: there is nothing left to disagree with.
    """
    return status_for_classification


def test_the_status_resolver_is_defined_once_not_per_strategy():
    """Drift guard: no strategy module may reintroduce its own resolver.

    The historical defect was two independent if-chains. Both call sites now
    import ``status_for_classification`` from ``app.constants``, and a new
    per-module copy here is exactly the regression this pins.
    """
    for module in (http_mod, browser_mod):
        assert not hasattr(module, "_status_for_classification"), (
            f"{module.__name__} reintroduced a per-module status resolver"
        )
    assert not hasattr(BrowserRenderer, "_status_for_classification"), (
        "BrowserRenderer reintroduced a per-instance status resolver"
    )

    # The call sites must route through the shared name, not a local copy.
    for module in (http_mod, browser_mod):
        source = inspect.getsource(module)
        assert "status_for_classification(" in source, (
            f"{module.__name__} no longer resolves classifications through the shared app.constants table"
        )


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


def test_every_classifiable_label_resolves_through_the_table(browser_resolver):
    """Every label the classifiers can emit takes its status from the one table.

    This is the direct migration of the pre-consolidation check that the HTTP and
    browser paths agree on every label: with one shared resolver the agreement
    is by construction, so the assertion is now that the resolver both paths use
    is the table itself.
    """
    labels = _classifiable_labels() + sorted(FAILED_CLASSIFICATIONS)
    labels += sorted(HTTP_BLOCKED_CLASSIFICATIONS | BROWSER_BLOCKED_CLASSIFICATIONS)
    labels += list(UNRECOGNIZED_LABELS)
    for label in labels:
        expected = app_constants.CLASSIFICATION_STATUS_BY_LABEL.get(
            label, app_constants.UNKNOWN_CLASSIFICATION_STATUS
        )
        assert status_for_classification(label) == expected
        assert browser_resolver(label) == expected


def test_unknown_classification_has_one_documented_default(browser_resolver):
    """A label nobody knows about must resolve to the documented default."""
    for label in UNRECOGNIZED_LABELS:
        status = status_for_classification(label)
        assert status == browser_resolver(label) == app_constants.UNKNOWN_CLASSIFICATION_STATUS
        # The safe default may never be "success": an unrecognized label has
        # not produced verified content.
        assert status != "success"


def test_known_group_statuses_are_unchanged(browser_resolver):
    """Inverse case: the working paths must keep their exact statuses."""
    assert status_for_classification("rendered") == "success"
    assert browser_resolver("rendered") == "success"
    # Conditional freshness is a success: the caller's validators proved it.
    assert status_for_classification("not_modified") == "success"
    assert app_constants.status_for_classification("not_modified") == "success"
    for classification in BROWSER_BLOCKED_CLASSIFICATIONS:
        assert browser_resolver(classification) == "blocked"
    for classification in HTTP_BLOCKED_CLASSIFICATIONS:
        assert status_for_classification(classification) == "blocked"
    for classification in FAILED_CLASSIFICATIONS:
        assert status_for_classification(classification) == "failed"
        assert browser_resolver(classification) == "failed"


class TestStatusForClassification:
    def test_http_status_not_found(self):
        from app.constants import status_for_classification

        status = status_for_classification("not_found")
        assert status == "failed"

    def test_http_status_site_error(self):
        from app.constants import status_for_classification

        status = status_for_classification("site_error")
        assert status == "failed"
