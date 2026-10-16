"""One sanctioned source of minted handles.

Request/job ids double as the only lookup credential for ``/v1/crawl/{id}``-style routes,
so they carry the full 128-bit UUID hex -- short ids are brute-forceable.
"""

from __future__ import annotations

import uuid

REQUEST_ID_PREFIX = "crawl_"


def new_job_id() -> str:
    """Mint a whole-site/async job id: full 32-hex UUID entropy."""
    return uuid.uuid4().hex


def new_request_id() -> str:
    """Mint a request handle: prefixed, full 32-hex UUID entropy."""
    return f"{REQUEST_ID_PREFIX}{uuid.uuid4().hex}"


def new_watch_id() -> str:
    """Mint a watch id: full 32-hex UUID entropy."""
    return uuid.uuid4().hex
