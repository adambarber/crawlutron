# Typing-budget reduction roadmap (undeclared records) — as of c5ab69d

> **SUPERSEDED (2026-11-07):** the typing budget reached ZERO and the budget
> table itself is gone — `tests/test_typing_budget.py` now carries no `_BUDGETS`
> for the app tree; its unconditional zero-assertions are the terminus (see the
> in-file comment "ALL batches landed (1-4): ZERO undeclared records, ZERO bare
> Any, no budget table left"). This roadmap is HISTORICAL: the "Remaining 198
> offenders" below counted app/ paths that no longer exist (the tree is now
> `packages/core/crawlutron/`), and the blockers section describes a tree state that has long
> since been resolved. Kept for history only — do not work from this list.

Baseline measured on a detached worktree at HEAD: **204/204**. After the first
verified increment (error envelope + discovery payload records), measurement is
**198/198** (budget lowered in the same commit). Work was done in a detached
worktree because the shared working tree is not at the "clean baseline" the task
described — see the blockers section. One commit, `c5ab69d`, is packaged as
`docs/typing-increment-1-c5ab69d.patch`.

## Doctrine (three remedies, exactly two legal moves)

1. **Domain record** (known key set, known value types) → declare a `TypedDict`
   in `packages/core/crawlutron/models/` (models live one-per-file per #4740). First-class, not counted.
2. **System type** (known class, unconstrained contents): `JsonValue` for a
   payload that really is arbitrary JSON; `bytes`/`str`. Genuine narrowing, not counted.
3. **Exit** (a record whose key set the code depends on, no annotation) → the
   counted defect.

Rules that bite (from memory #4666 and the previous failed pass):
- Never type `JsonValue` at a site that then does `.get()/.items()/**` without a
  guarded local narrowing — mypy/basedpyright go red.
- `dict[str, str]` / `dict[str, int]` (open key set, typed value) are NOT counted.
- **JsonValue cannot be a pydantic `BaseModel` field** without `RecursionError`
  at schema generation (the model is used as a FastAPI `response_model` / `model`).
  This is a real technical blocker for the `ErrorResponse.details` field below.
- A consumed `dict[str, Any]` whose caller is one of the 8 files the parallel
  agent owns cannot be retyped without editing that forbidden file (mypy breaks
  the forbidden caller). Those are blocked until the parallel agent lands.

## First increment (done, verified, committed c5ab69d)

| File | Position | Move |
|---|---|---|
| `packages/core/crawlutron/models/error_response.py` | `ErrorEnvelope` (new TypedDict) | record |
| `packages/rest/crawlutron_rest/api/exceptions.py:18,32,52` | `error_envelope`/`error_envelope_from_detail`/`http_error` | return `ErrorEnvelope`; `details` → `JsonValue`; guarded `cast` at pydantic validation |
| `packages/core/crawlutron/models/discovery_response.py` | `DiscoveryPayload` (new TypedDict) | record |
| `packages/core/crawlutron/discovery/well_known.py:11,62` | `discovery_payload`/handler | return `DiscoveryPayload` seed identity keys explicitly |

Verified: mypy clean, ruff check + format clean, docstring gate, file-size,
duplication, typing-budget gate all green (run from the main venv against the
worktree; the worktree has no `.venv`, so the pre-commit hooks cannot run there —
`--no-verify` documented in the commit).

## Remaining 198 offenders, classified per module

Entanglement is the dominant blocker: many `packages/core/crawlutron/` factories/tables are consumed
from the 8 forbidden files, so retyping a value the forbidden file then key-reads
or holds forces an edit there. Every module below is marked **doable now** or
**blocked by <forbidden file>**.

### app/discovery — doable now, like the first increment
- `capabilities.py:46,131` (2): `capabilities_payload` builds a fixed record →
  `CapabilitiesPayload` TypedDict alongside `CapabilitiesResponse`; spread the
  identity keys explicitly (the `**SERVICE_IDENTITY`-into-TypedDict trick that
  worked for `DiscoveryPayload`). Router caller is internal.
- `agent_hints.py:105,122` (2) + `packages/core/crawlutron/models/agent_hints_response.py` (1):
  `agent_hints_payload` → `AgentHintsPayload` TypedDict; `example_policies`
  values are arbitrary nested policy JSON → `dict[str, JsonValue]`.
  Caveat: `AgentHintsResponse.example_policies: dict[str, dict[str, Any]]` is a
  pydantic field, so leave the field as `dict[str, Any]` (JsonValue breaks schema)
  and only retype the raw-payload TypedDict.

### app/admission — mixed
- `payload_helpers.py:27,58,90` (3): the admission wire payload (`urls`/`url`/
  `policy`) is the textbook record the task named. **Blocked**: it is threaded
  through `packages/rest/crawlutron_rest/api/crawl_endpoint.py:115` (forbidden). Retype the helper params to
  `AdmissionWire` TypedDict only after the parallel agent lands, else mypy breaks
  crawl_endpoint.
- `policy.py:159-342` (13): the policy repair machinery operates on arbitrary
  nested JSON → per task guidance, `JsonValue` for the wire param with guarded
  local narrowing where it reads keys. `CrawlPolicy.applied()`'s
  `{timeout_seconds, max_retries}` → a small TypedDict. **Check entanglements**:
  policy is consumed widely; several `dict[str, Any]` positions are return records
  that routers read — likely doable for the JsonValue/spill parts, blocked where
  a forbidden consumer key-reads.
- `render_options.py:39,49,50,65` (5): `_pick`/`resolve_render_options` read a
  fixed key set (`timeout_seconds`,`max_retries`,`respect_robots`,`include_html`,
  `include_text`,`browser_profile`) → a `RenderOptionsWire` TypedDict. Callers are
  `render.py` and `browser_result.py` (not forbidden) → **doable now**. Note the
  resolver ignores unknown keys (open record), so a TypedDict with optional fields
  is the honest shape; `dict[*, ...]` inputs from callers may need adaptation.

### app/api — mostly blocked / needs records
- `crawl_executor.py:24,49` (2), `exceptions.py` done, `middleware.py:49` (1,
  ASGI `MutableMapping[str, Any]` — framework type read by key; leave: it is the
  ASGI message contract, not an app record; a narrow `http.request` TypedDict is
  possible but low value), `site_map.py:21` (1, `build_site_map` returns a fixed
  record → `SiteMapPayload` TypedDict; caller is a router, **doable now**),
  `unified_shaping.py:118,184,193` + 1 (4, reads a known result shape → record;
  check caller entanglement), `webhooks.py:116`, `render.py:36,68` (2),
  `map_endpoint.py:23`, `map_router.py:29`, `response_shaping.py:169`.
  `crawl_endpoint.py:77` — forbidden.
- **Blocked**: endpoint handlers that feed the forbidden `crawl_endpoint.py`.

### app/models — records and pydantic-field limits
- `crawl_policy.py:105,113,168,173,220` (7): `CrawlPolicy` internals. Where a
  value is a pydantic field (e.g. `browser_profile`), JsonValue breaks schema;
  where it is a helper return record, use a TypedDict. Moderate; check forbidden
  consumers (`http_classify` is forbidden and reads classification evidence).
- `crawl_result.py:60,116,117,129` (4), `unified_crawl_result.py:90` (1),
  `page_metadata.py:42`, `page_metadata_response.py:27`: result records.
- `error_response.py:26` (1): `ErrorResponse.details: dict[str, Any]` — **blocked
  by pydantic schema recursion**: JsonValue as a BaseModel field raises
  RecursionError. Needs a main-thread decision (recursive pydantic model behind a
  `@serializer`, or a `JsonValue`-shaped union pydantic can schema, or a
  justification for leaving it as the single counted site).
- `serialization.py:30,104,152,161` (4): `to_dict`/`from_wire`/`_rebuild_dataclass`.
  `to_dict -> dict[str, Any]` is a JSON-safe record → could be `JsonValue`; the
  `Mapping[str, Any]` payloads are read generically over the dataclass's fields,
  which is structural reflection (not a declared key set) → keep as the 
  "untyped door" it documents itself to be, or narrow with a guarded read. Low
  priority; central and risky.

### app/extraction
- `classify.py:147,178` — forbidden.
- `output_formats.py:52,75,136` (3): `serialize_results`/`_to_markdown`/`_to_csv`
  read the unified result by key (`final_url`,`status`,...) → a `CrawlResultWire`
  TypedDict. **Blocked**: sole caller is `packages/rest/crawlutron_rest/api/crawl_endpoint.py:92` (forbidden).
- `trees.py:121-208` (6), `discovery.py:21,37,89` (3), `llm_format.py:105`,
  `document_text.py`/`documents.py`: DOM/tree node records; the list members are
  output records → TypedDicts. Moderate, check forbiddden consumers.
- `http_classify.py` — forbidden.

### app/mcp — doable (protocol types)
- `protocol.py:58,65` (2), `tools.py:79,85,120,246` (4). These already use
  `JsonValue` elsewhere; the `dict[str, Any]` remnants that are MCP JSON-RPC
  params/results → `JsonValue` / records. `packages/core/crawlutron/mcp/server.py` is not forbidden.
  Check `packages/core/crawlutron/mcp/server.py:216` (1) — not in the forbidden list, doable.
- `well_known.py` done.

### app/orchestration
- `engine_result.py` (7) — forbidden.
- `doc_pool.py:88` (1) — forbidden.
- `robots_parse.py:74..140` (5): robots directives parse arbitrary text lines; the
  `list[Any]`/`tuple` positions are parse-internal records → TypedDict or JsonValue
  per site. `request_pipeline.py:64`, `render_result.py:36`,
  `strategy_chain.py:143`, `engine.py:390` (records / JsonValue). Check engine
  consumers.
- `fetch_helpers.py`, `scheduler.py` — check for forbidden entanglement (metrics is forbidden).

### app/storage
- `result_store.py:45,67,100,104` (4): `put`/`get`/`aput`/`aget` genuinely
  pass-through arbitrary stored JSON (`json.dumps`/`json.loads`) → **JsonValue**.
  **Blocked**: `aget` return is key-read by `packages/rest/crawlutron_rest/api/crawl_endpoint.py:179`
  (forbidden) and `aput` is fed by `crawl_endpoint.py:317`; retyping to JsonValue
  breaks those without narrowing edits in the forbidden file.
- `job_store_state.py` (5) — forbidden.
- `job_store_collections.py:54` (1): a store record → TypedDict, **doable**.

### app/transport
- `browser_driver.py` (21), `driver_config.py` (10): CDP/browser protocol dicts
  read by key. Browser-DOM profile structures → records, or JsonValue where the
  CDP payload is arbitrary. Heavy; check forbidden `request_profile`/`http_classify`
  interplay.
- `http_classify` (2), `request_profile` (5) — forbidden.
- `content_pipeline.py` (7), `browser_result.py` (6), `http_response.py` (5),
  `fetch_policy.py` (5), `fetch_policy.py:51,104...`, `http.py:47,88`,
  `driver_pages.py:142,144`, `browser.py:253,314`, `browser_targets.py:169,189`,
  `pooled_clients.py:225`, `http_cache.py:120`, `http_client_pool.py:61`,
  `body_decode.py:69`, `document.py:25`, `content_cache` etc.: mix of transport
  records and pass-through payloads; judge per site.
- `http_cache.py:9` references `policy_wants_html` (render_options) — keep consistent.

## Blockers requiring the main thread

1. **Shared tree not clean.** 28 app files carry uncommitted, partially broken
   work by another actor (admission_links.py has a SyntaxError:107 that stops the
   typing gate from running at all; settings/site_map/browser_targets show
   wholesale docstring/comment stripping incl. the 8 protected files). I did NOT
   `git checkout -- app/` for fear of destroying that work. Landing anything
   requires either committing my worktree patch before/after the parallel agent's
   work, or serializing agents.
2. **Budget is one global number** in `tests/test_typing_budget.py`; concurrent
   agents editing `_BUDGETS` and the `collector_sees_the_whole_tree` floor will
   conflict unless commits are serialized. Both moved in the same commit here.
3. **JsonValue cannot be a pydantic BaseModel field** (schema RecursionError).
   Any `dict[str, Any]` field on a pydantic model (ErrorResponse.details,
   AgentHintsResponse.example_policies, CrawlPolicy internals) is not a simple
   JsonValue swap.
4. **Parallel scope commits 8 files**: engine_result, doc_pool, request_profile,
   http_classify, classify, crawl_endpoint, job_store_state, metrics are both
   offenders and callers of values I otherwise can't retype. They carry ~23
   offenders and block ~6 more clusters via consumer entanglement.
5. **`-Collector floor` (`{"annotation": 200}`)** in test_typing_budget.py must
   track the shrinking candidate pool; it was lowered to 190 with c5ab69d and must
   keep being lowered as work proceeds (it is a proof, not a budget — set it just
   under the live candidate count).