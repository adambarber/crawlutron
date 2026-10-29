"""The status-override evidence chain is pinned classification AND evidence (#330).

The five if/elif branches each set (classification, evidence); the table rewrite
must produce both byte-identically, evidence lists included.
"""

from __future__ import annotations

import dataclasses

import pytest

from crawlutron.transport.http_classify import classify_extracted_content


@dataclasses.dataclass
class _Resp:
    status_code: int
    content_type: str = "text/html"


class _Ctx:
    extraction = "http_html"


class _Extracted:
    html = "<html><body>hi</body></html>"
    text = "hi"
    content_type = "text/html"


def _evidence(classification: str, signature: str, status: int) -> list[dict]:
    return [
        {
            "classification": classification,
            "signature": signature,
            "source": "http_status",
            "context": f"HTTP {status}",
        }
    ]


_EVIDENCE = {
    403: _evidence("access_denied", "403", 403),
    404: _evidence("not_found", "404", 404),
    410: _evidence("gone", "410", 410),
    429: _evidence("rate_limited", "429", 429),
    503: _evidence("site_error", "503", 503),
}


@pytest.mark.parametrize("status", sorted(_EVIDENCE))
def test_status_override_evidence_is_pinned(status: int) -> None:
    classification, evidence = classify_extracted_content(_Ctx(), _Resp(status), _Extracted())  # type: ignore[arg-type]
    assert classification == _EVIDENCE[status][0]["classification"]
    assert evidence == _EVIDENCE[status]
