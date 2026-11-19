"""Median-of-N benchmarks for crawlutron_extract extraction timings."""

from __future__ import annotations

import statistics
import time
import zipfile
from io import BytesIO
from pathlib import Path

from crawlutron_extract import extract
from crawlutron_extract.schema import FieldKind, FieldSpec, Schema, extract_records

REPEATS = 7
DOCX_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
GOLDEN = Path(__file__).resolve().parent.parent / "tests" / "golden"


def _median_n(fn, n: int = REPEATS) -> float:
    fn()  # warmup
    times: list[float] = []
    for _ in range(n):
        start = time.perf_counter()
        fn()
        times.append(time.perf_counter() - start)
    return statistics.median(times)


def _html_page(size_bytes: int) -> str:
    """Deterministic HTML of roughly ``size_bytes`` bytes."""
    parts = ["<html><body><h1>Bench page</h1>"]
    paragraph = "<p>The quick brown fox jumps over the lazy dog. </p>"
    while sum(len(p.encode("utf-8")) for p in parts) < size_bytes:
        parts.append(paragraph)
    parts.append("</body></html>")
    return "".join(parts)


def _build_docx(paragraphs: int) -> bytes:
    """Minimal DOCX with ``paragraphs`` paragraphs (same shape as the test fixtures)."""
    rows = "".join(
        f"<w:p><w:r><w:t>bench paragraph {i}: alpha beta gamma delta epsilon</w:t></w:r></w:p>"
        for i in range(paragraphs)
    )
    document = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>" + rows + "</w:body></w:document>"
    )
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        )
        zf.writestr("word/document.xml", document)
    return buffer.getvalue()


def _case(name: str, content_type: str, fn) -> tuple[str, str, float]:
    elapsed = _median_n(fn)
    return name, content_type, elapsed


def main() -> None:
    rows = []
    html_100k = _html_page(100 * 1024).encode("utf-8")
    rows.append(_case("HTML 100 KiB", "text/html", lambda: extract(html_100k, "text/html")))
    html_1mib = _html_page(1024 * 1024).encode("utf-8")
    rows.append(_case("HTML 1 MiB", "text/html", lambda: extract(html_1mib, "text/html")))
    rows.append(_case("DOCX small", DOCX_CT, lambda: extract(_build_docx(5), DOCX_CT)))
    rows.append(_case("DOCX large", DOCX_CT, lambda: extract(_build_docx(200), DOCX_CT)))
    pdf_bytes = (GOLDEN / "simple1.pdf").read_bytes()
    rows.append(_case("PDF real (golden)", "application/pdf", lambda: extract(pdf_bytes, "application/pdf")))
    schema = Schema(
        record="article",
        fields=(
            FieldSpec("title", "h1", FieldKind.TEXT),
            FieldSpec("body", "p", FieldKind.TEXT, many=True),
        ),
    )
    rows.append(
        _case(
            "schema extraction",
            "text/html",
            lambda: extract_records(_html_page(50 * 1024), schema),
        )
    )

    print("crawlutron-extract bench (median of", REPEATS, "repeats)")
    print()
    print(f"{'Case':<24} {'Content type':<48} {'median (s)'}")
    print("-" * 78)
    for name, ctype, elapsed in rows:
        print(f"{name:<24} {ctype:<48} {elapsed:.6f}")
    print()
    print("Slowest:", max(rows, key=lambda r: r[2])[0])


if __name__ == "__main__":
    main()
