"""Classification test data shared by the taxonomy and classification modules.

Declared support surface (#476): moved verbatim from
test_classification.py so a sibling test module imports these from here,
not from another test's internals.
"""

from __future__ import annotations

from app.vocabulary.taxonomy import (
    BROWSER_BLOCKED_CLASSIFICATIONS,
    FAILED_CLASSIFICATIONS,
    HTTP_BLOCKED_CLASSIFICATIONS,
)

__all__ = ["GROUPING_ASSIGNMENTS", "GROUPINGS", "UNRECOGNIZED_LABELS"]

# Labels that are not in the taxonomy at all: this is exactly where the two historical implementations
# disagreed.
UNRECOGNIZED_LABELS = ("totally_new_label", "some_future_block", "")

# The classification groupings are set OPERATIONS over one another, not one transcription of the taxonomy per
# grouping.
GROUPING_ASSIGNMENTS = frozenset(
    {
        "BROWSER_BLOCKED_CLASSIFICATIONS",
        "HTTP_BLOCKED_CLASSIFICATIONS",
        "FAILED_CLASSIFICATIONS",
        "TRANSIENT_CLASSIFICATIONS",
        "NON_TRANSIENT_CLASSIFICATIONS",
    }
)

GROUPINGS = (
    HTTP_BLOCKED_CLASSIFICATIONS,
    BROWSER_BLOCKED_CLASSIFICATIONS,
    FAILED_CLASSIFICATIONS,
)
