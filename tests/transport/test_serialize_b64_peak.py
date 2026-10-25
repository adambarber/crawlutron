"""#355: ``serialize_response`` must not hold the body and the full b64 string at once.

The one-shot encode materialised b64 bytes + ascii str alongside the body; a
chunked encode into one str keeps the peak near body + output.
"""

from __future__ import annotations

import base64
import dataclasses
import tracemalloc

from app.transport.http_response import serialize_response


@dataclasses.dataclass
class _Resp:
    """Minimal fetcher-response shape ``serialize_response`` reads."""

    body: bytes
    status_code: int = 200
    headers: dict[str, str] = dataclasses.field(
        default_factory=lambda: {"content-type": "application/octet-stream"}
    )
    final_url: str = "http://example.com/final"
    elapsed_ms: int = 5


def test_b64_encode_peak_under_2_5x_body():
    """A 1 MiB body encodes with peak < 2.5x the body size (old shape ~3.5x)."""
    n = 1024 * 1024
    body = b"x" * n  # allocated before the probe; encode-time allocations are traced
    tracemalloc.start()
    envelope = serialize_response(_Resp(body=body))
    _cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert len(envelope["body_b64"]) == 4 * ((n + 2) // 3)
    assert envelope["body_b64"] == base64.b64encode(body).decode("ascii")
    assert peak < 2.5 * n, f"peak {peak} vs body {n}"
