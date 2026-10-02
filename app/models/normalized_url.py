"""The canonical form of a URL that has crossed the normalizer.

A ``NewType`` over ``str``: erased at runtime -- spill JSON, cache keys, and the
wire keep plain strings -- but mypy rejects putting a raw request/sitemap/crawl
string into a normalized-URL position unless it has crossed
``app.validation.normalize_public_http_url``. That is the whole point of the
nominal wrapper: it is the one place the tree can say "this string has been
admitted", and every position that admits a URL for queueing can say so in its
own signature.
"""

from __future__ import annotations

from typing import NewType

NormalizedUrl = NewType("NormalizedUrl", str)
