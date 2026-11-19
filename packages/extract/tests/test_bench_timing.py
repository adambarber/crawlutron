"""Coarse timing guard: exactly one timing assertion in the suite."""

from __future__ import annotations

import time

from crawlutron_extract import extract


def _big_html(size_bytes: int) -> bytes:
    block = "<p>The quick brown fox jumps over the lazy dog.</p>"
    repeat = size_bytes // len(block.encode("utf-8")) + 1
    html = "<html><body>" + block * repeat + "</body></html>"
    return html.encode("utf-8")[:size_bytes]


def test_1mib_html_extract_under_two_seconds() -> None:
    """1 MiB HTML extracts under 2 s (coarse regression floor)."""
    html = _big_html(1024 * 1024)
    total = 0.0
    for _ in range(5):
        start = time.perf_counter()
        extract(html, "text/html")
        total += time.perf_counter() - start
    assert total / 5 < 2.0
