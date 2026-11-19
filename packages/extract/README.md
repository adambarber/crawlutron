# crawlutron-extract

Content extraction: HTML, office containers (DOCX/XLSX/PPTX/EPUB),
text formats (CSV/JSON/XML), and a PDF tier-1 text sweep with pdfminer.six
fallback for low-quality pages. Bounded, deterministic,
never raises on hostile input.

Links are admitted through `crawlutron-safe-url` (`NormalizedUrl`), syntax-checked
and refused when unsafe; DNS-level pinning is done by the caller
(`resolve_and_pin_host`), not by the extractor.

The dependency closure is `crawlutron-safe-url` (which itself brings `regex`
(bounded matcher) and `publicsuffixlist` (public suffix list) as hard
dependencies) and `pdfminer.six` (PDF tier-1 fallback). `crawlutron-extract`
ships no extras and is not stdlib-only.
