"""May this page be fetched at all: the admission layer.

Every URL a crawl touches passes here -- permitted, in scope, worth the budget. ``read_policy``
is the one read edge; whole-site and recursive admission are separate modules on purpose.
"""

from __future__ import annotations
