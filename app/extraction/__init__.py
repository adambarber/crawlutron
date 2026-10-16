"""How bytes become text: the extraction layer.

Every strategy's bytes arrive here and leave as a consistent ``(text, markdown)`` pair;
``content``/``classify`` carry the work, ``output_formats`` shapes the result for the caller.
"""

from __future__ import annotations
