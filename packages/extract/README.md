# crawlutron-extract

Stdlib-only content extraction: HTML, office containers (DOCX/XLSX/PPTX/EPUB),
text formats (CSV/JSON/XML), and a PDF tier-1 text sweep. Bounded, deterministic,
never raises on hostile input. The optional `pdf-fallback` extra adds pdfminer.six
behind a lazy seam, invoked only when tier 1 reports `quality="low"`.
