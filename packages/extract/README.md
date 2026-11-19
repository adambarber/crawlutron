# crawlutron-extract

Bounded, deterministic, never-raise content extraction. Converts a byte stream plus a
content type into a single `Extraction` payload — capped text/markdown, links, frames,
forms, tables, schema records, a quality grade, and degradation flags — for HTML, office
containers (DOCX/XLSX/PPTX/EPUB), text formats (CSV/JSON/XML), and a PDF tier-1 text
sweep via pdfminer.six.

There is exactly one entry point, `extract()`. Everything else is derived from its
`Extraction` result.

## The single entry point

```python
from crawlutron_extract import extract, DEFAULT_LIMITS
from crawlutron_extract.models import Extraction

result: Extraction = extract(
    content: bytes,
    content_type: str,
    *,
    url: str | None = None,
    limits: ExtractLimits = DEFAULT_LIMITS,
)
```

`extract` is the single entry point for every format. It:

* never raises — hostile or malformed input yields `quality="failed"` plus a flag,
* refuses input above `limits.max_input_bytes` (`input_refused`),
* dispatches by content type to the HTML, office, PDF, or text-format path,
* returns a fresh, deterministic `Extraction` on every call (two calls on the same bytes
  are byte-identical).

Links are admitted through `crawlutron-safe-url` (`NormalizedUrl`), syntax-checked and
refused when unsafe; DNS-level pinning is the caller's job, not the extractor's.

## Public types

All public symbols live in `crawlutron_extract` (see `__all__`).

**`Extraction`** — the bounded, deterministic result payload:
* `text: str` — extracted body text, capped at `max_text_chars`
* `markdown: str` — markdown rendering, capped at `max_markdown_chars`
* `charset: str | None` — detected charset for text/HTML
* `metadata: dict[str, str]` — content type, charset, url, container/format hints
* `links: list[NormalizedUrl]` — admitted links, capped at `max_links`
* `frames: list[dict[str, str]]` — frames, capped at `max_frames`
* `forms: list[dict]` — forms, capped at `max_forms`
* `tables: list[list[list[str]]]` — tables as row/cell grids, capped at `max_tables`
* `quality: Quality` — `HIGH` / `MEDIUM` / `LOW` / `FAILED`
* `flags: list[str]` — degradation signals, never data loss
* `truncated: bool` — any output list was capped
* `links_refused: int` — link count refused by the URL policy
* `records: tuple[Mapping[str, object], ...]` — schema records (schema path only)

**`Quality`** (`StrEnum`) — closed ladder: `HIGH`, `MEDIUM`, `LOW`, `FAILED`.

**`ExtractLimits`** (frozen dataclass) — every cap applied to an extraction; see
"Limits and defaults" below. `DEFAULT_LIMITS` is the module-level instance.

**Schema types** (`crawlutron_extract.schema`):
* `FieldSpec(name, selector, kind=FieldKind.TEXT, attribute=None, required=False, many=False)`
* `FieldKind` — `TEXT`, `MARKDOWN`, `ATTR`, `LINK`, `NUMBER`, `INTEGER`
* `Schema(record: str | None, fields: tuple[FieldSpec, ...])`
* `extract_records(html, schema, *, url=None, limits=None) -> Extraction`

**Selectors**: `validate_css(selector)`, `validate_xpath(expr)`, both raise
`SelectorError` when an expression is outside the supported subset.

Other public doors: `extract_discovered_links`, `extract_js_endpoints`,
`extract_sitemap_locs`, `html_diagnostics`, `detect_pagination`, `LinkRecord`.

## Schema language with a worked example

The schema path extracts structured records from HTML using a closed CSS-selector
subset. A `Schema` pairs an optional record selector with a tuple of `FieldSpec`s. When
`record` is `None` the whole document is one record.

```python
from crawlutron_extract import DEFAULT_LIMITS, extract_records
from crawlutron_extract.schema import FieldKind, FieldSpec, Schema

schema = Schema(
    record="article.product",
    fields=(
        FieldSpec("title", "h1", FieldKind.TEXT),
        FieldSpec("price", "span.price", FieldKind.NUMBER),
        FieldSpec("link", "a", FieldKind.LINK, many=True),
        FieldSpec("in_stock", ".stock", FieldKind.TEXT),
    ),
)
result = extract_records(html_bytes, schema, url="https://example.com/", limits=DEFAULT_LIMITS)
# result.records -> tuple of dicts like {"title": "...", "price": 12.5, "link": (...), ...}
```

`FieldKind.LINK` resolves and admits each `href` through `crawlutron-safe-url`; refused
links are dropped. `NUMBER`/`INTEGER` coerce text; a missing required field sets
`schema_required_missing`. Output is bounded by `max_records` and `max_fields_per_record`.

## CSS subset (closed — refused outside)

`validate_css` accepts only this subset; anything else raises `SelectorError`:

* type selector (`div`), universal (`*`)
* class (`.c`) and id (`#i`)
* attribute presence (`[a]`) and equality / prefix / suffix / contains
  (`[a='v']`, `[a^='v']`, `[a$='v']`, `[a*='v']`)
* combinators: descendant (whitespace), child (`>`), adjacent (`+`)
* pseudo-classes: `:first-child`, `:last-child`, `:nth-child(n)`, `:not(s)`
* comma-separated selector lists

Refused (outside the subset, raises `SelectorError`): namespaces, pseudo-elements (`::`),
`:hover`/`:nth-of-type`/`:has`/`:is`/`:where` and any pseudo-class not listed, attribute
operators other than `= ^= $= *=`, and `:not()` containing a non-simple selector. The
subset is closed by design: if `validate_css` accepts it, the evaluator can match it; if
the evaluator cannot match it, `validate_css` rejects it.

## XPath subset (closed — refused outside)

`validate_xpath` accepts only this subset; anything else raises `SelectorError` naming the
offending token:

* location steps: `/`, `//`, `.//`
* node test: `*`, a tag name, `text()`, `@attr`
* predicates: `[n]`, `[last()]`, `[@a]`, `[@a='v']`
* functions: `contains()`, `concat()`, `normalize-space()`, `starts-with()`

Refused (outside the subset): axes (`self::`, `parent::`, `child::`, `descendant::`,
`attribute::` and any other), any function not listed, any predicate form not listed, and
any unknown character or token. The subset is closed: `validate_xpath` rejects everything
the bounded evaluator cannot execute.

## Limits and defaults

`ExtractLimits` (frozen dataclass), defaults shown. `max_input_bytes` refuses oversized
input before any decode; every list output is capped and sets `truncated`.

| Limit | Default | Meaning |
|---|---|---|
| `max_input_bytes` | 50,000,000 | refuse input larger than this (50 MB) |
| `max_text_chars` | 100,000 | cap on extracted `text` |
| `max_markdown_chars` | 200,000 | cap on extracted `markdown` |
| `max_links` | 1,000 | cap on admitted links |
| `max_tables` | 50 | cap on tables |
| `max_table_rows` | 1,000 | rows per table |
| `max_table_cols` | 100 | columns per table |
| `max_frames` | 100 | cap on frames |
| `max_forms` | 100 | cap on forms |
| `max_dom_nodes` | 50,000 | DOM node cap (HTML/schema path) |
| `max_dom_depth` | 50 | DOM depth cap |
| `max_records` | 10,000 | schema records cap |
| `max_fields_per_record` | 100 | fields per schema record cap |
| `max_zip_entries` | 10,000 | office container entry cap |
| `max_zip_ratio` | 1,000 | office container compression-bomb ratio cap |

`RICH_TEXT_FLOOR = 200` is the minimum body-text length for `quality="high"` (requires a
main-content element and a non-lossy decode).

## Quality ladder

Closed vocabulary, evaluated by `_resolve_quality`:

| Grade | Meaning |
|---|---|
| `HIGH` | main-content element found, body text ≥ `RICH_TEXT_FLOOR`, decode not lossy |
| `MEDIUM` | body text present, but below the floor or a lossy decode |
| `LOW` | decoded text or structural content (links/forms/tables) present, no body text / lossy |
| `FAILED` | no extractable content, refused format, or input refused |

A clean non-HTML document with decoded text is `LOW` (no main-content concept). An empty
result is never above `LOW`.

## Flags vocabulary (degradation, not failure)

Flags describe degradation, never data loss. `extract` never returns partial data silently:
every flag is paired with the result it qualifies, and `truncated` is set whenever a cap
bit. Representative flags:

* `charset_lossy` — text had to be lossily re-encoded
* `links_refused` — one or more links refused by the URL policy
* `input_refused` — input exceeded `max_input_bytes`
* `format_refused` — content type not supported
* `selector_invalid` / `schema_required_missing` — schema selector or required field issue
* `records_truncated` / `fields_truncated` — a schema cap was hit
* `schema_dom_cap` — the DOM cap prevented schema extraction
* `pdf_encoding_partial` — PDF text sweep could not recover the full encoding
* `table_grid_degraded` / `nested_table_flattened` — a table was normalized, not dropped

Text is never discarded because markdown cannot express a shape: nested tables flatten into
the enclosing cell (pipes escaped, `nested_table_flattened` set), `colspan` repeats content
and `rowspan` carries it down, and the grid stays rectangular.

## Hard dependency set

`crawlutron-extract` ships no extras and is not stdlib-only. The hard dependencies are
imported plainly — a broken install fails at import, there is no lazy/fallback path:

* `crawlutron-safe-url` — `NormalizedUrl` and `HostPolicy`; its own closure is also hard:
  * `regex` — bounded matcher
  * `publicsuffixlist` — public suffix list
* `pdfminer.six` — PDF tier-1 text sweep

Credentials and URLs are object-invariant: a `NormalizedUrl` is the sole validation
chokepoint (it rejects userinfo), and secrets never surface unless intentionally consumed.

## Deliberate non-features

* No OCR — scanned or image-only PDFs/pages yield no text.
* No JS execution — script content is never executed; JS endpoints are surfaced as text or
  links only, never evaluated.
* No network I/O — extraction is a pure `bytes` → `Extraction` function; DNS resolution and
  host pinning are the caller's responsibility.
* No DOM for the streaming path — text formats (CSV/JSON/XML) are swept single-pass
  without building a DOM; the HTML/schema path builds a bounded DOM under `max_dom_nodes`.
* PDF tables are reconstructed from layout rather than a full PDF object model — pdfminer.six
  supplies the tier-1 text sweep; there is no general PDF DOM.

## Guarantees

Bounded (every list capped, input size refused), deterministic (same bytes ⇒ same result),
never raises (hostile input ⇒ `quality="failed"`), and closed (CSS and XPath subsets are
refused outside, never silently extended).
