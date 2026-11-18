# crawlutron-extract

Content extraction: HTML, office containers (DOCX/XLSX/PPTX/EPUB),
text formats (CSV/JSON/XML), and a PDF tier-1 text sweep. Bounded, deterministic,
never raises on hostile input. pdfminer.six is imported lazily behind a seam,
invoked only when tier 1 reports `quality="low"`; it is not part of the default install.

Links are admitted through `crawlutron-safe-url` (`NormalizedUrl`), syntax-checked
and refused when unsafe; DNS-level pinning is done by the caller
(`resolve_and_pin_host`), not by the extractor.

The `crawlutron-safe-url` dependency brings `regex` (bounded matcher) and
`publicsuffixlist` (public suffix list) into the dependency closure as hard
dependencies; `crawlutron-extract` is not stdlib-only and ships no extras.
