# Bench results — crawlutron-extract

Median-of-7 extraction timings from `packages/extract/bench/bench_extract.py`.

Machine: MacBook-Pro.local, Python 3.14.10 on darwin
Date: 2026-11-18

| Case | Input | Median (s) |
|---|---|---|
| HTML 100 KiB | ~100 KiB text/html | 0.007148 |
| HTML 1 MiB | ~1 MiB text/html | 0.074795 |
| DOCX small | 5 paragraphs | 0.000055 |
| DOCX large | 200 paragraphs | 0.000473 |
| PDF real (golden) | simple1.pdf (849 B) | 0.000024 |
| schema extraction | ~50 KiB HTML, 2 fields | 0.019998 |

Coarse timing gate: 1 MiB HTML under 2 s (observed 0.074795 s). This is the
suite's only timing assertion; timing is inherently flaky, so the bound is
deliberately coarse and no other timing assertions exist.
