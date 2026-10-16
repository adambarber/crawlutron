"""Every name in ``app/`` reached privately, declared with its kind and reason: a
gate that weighs a reach's merit is a decaying convention, so any new weld must be
written down here -- and the names are verified, only the reasons are hand-maintained.
"""

from __future__ import annotations

from tests._private_import_decl import Declaration

# Every private reach, keyed `module.name` across both trees.
from tests._private_import_rows_a import DECLARED as _ROWS_A
from tests._private_import_rows_b import DECLARED as _ROWS_B

DECLARED: dict[str, Declaration] = {**_ROWS_A, **_ROWS_B}
