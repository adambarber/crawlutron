# Safety property inventory

**Purpose.** This codebase kept rediscovering the same defect *classes* because
gates were written after incidents. This file inverts that: every safety
property the service has is named here, with its owner, the seam its
enforcement lives at, and the gate that fails when it is violated. A property
with no gate is not an oversight waiting to be discovered — it is the next
incident, already knowable.

**How to use it.** The remaining work waves do not ask "what broke this time?"
They ask "which entry on the priority list does this wave close?" New code that
implements a policy must name the property it belongs to; a module that
implements a property without inheriting its gate is a review finding by
construction. When a property's status below is wrong, fixing the inventory is
part of fixing the code — a gate that is weaker than its claim makes the claim
worthless.

**Status vocabulary.**

- `GATED` — a gate exists and covers the property's whole surface.
- `PARTIAL` — a gate exists but covers only part of the surface; the uncovered
  part is named.
- `UNGATED` — no gate. The property is enforced only by reviewer attention.

**Seam vocabulary** (strongest first). A gate at a *runtime construction* seam
cannot be bypassed by a caller at all; a *signature/type* gate fails to
type-check when omitted; an *AST route* gate fails CI when a known route is
violated; a *config* gate is a bound or a pinned value. The recurring failure in
this codebase is a property owned at a construction seam but policed by an AST
route gate, which is why the same defect keeps reappearing at a new call site.

---

## A. Network boundary and page identity

| # | Property (stated as a rule) | Owner | Seam | Gate | Status |
|---|---|---|---|---|---|
| A1 | Canonical page identity is computed by one function, and every frontier key, visited entry, cache key and fetch target derives from it — never from a raw submitted string | `app/models/normalized_url.py:NormalizedUrl` (constructor; `.raw` is the durable identity), storage-read admission in `app/storage/job_store.py` | runtime construction | `test_page_identity_fallback_is_not_written_out_at_call_sites` (AST, tree-wide, with a bite proof) + the owner's contract test; `test_batch_dedup_keys_on_admitted_identity` (AST, tree-wide); job-store normalization tests | `GATED` |
| A2 | The public-address verdict (private / loopback / link-local / reserved / CGNAT / NAT64 / ULA / multicast) has exactly one implementation | `app/models/address_class.py:classify` | runtime construction | property registry `public-target` + reserved-range corpus test + classification table + bite proof | `GATED`, weak seam (see W1) |
| A3 | The host allow/deny policy cannot be omitted by any path that normalizes or admits a URL | `app/safety/validation.py:_host_policy_reason` | **signature (required argument)** + runtime refusal | `tests/crawl/test_ssrf.py::TestFrontierIdentityRefusesRejectedTargets` — asserts every entry shape yields no identity on refusal, for both refusal reasons, and that a public target still gets one | `GATED` — corrected 2026-09-30. The admission decision itself was already fail-closed: `_candidate_stream:131` and `normalize_sitemap_candidates:415` both drop on `None`. The earlier note claiming a denied target was "persisted, scheduled and reported as accepted (202)" overstated it — that path never reached the accept branch. What was real: `_frontier_identity:114` returned `identity or raw_url`, so a refused target still got a string back. Its only production caller uses it to build a dedupe reservation set, so the consequence was an inert reservation rather than an SSRF — but it made a refusal indistinguishable from an acceptance, which is the defect the property forbids. Fixed; the raw string is no longer returned |
| A3b | **Every** URL entry point applies the private-target refusal, not only the fetch path | `app/safety/validation.py` (`admit_or_none`, `filter_js_endpoints`) | signature | `tests/crawl/test_ssrf.py::TestFrontierIdentityRefusesRejectedTargets` for the admission seam; **no gate yet for the egress doors** | `PARTIAL` — corrected 2026-09-30. The earlier claim, that these "pass private URLs even with an allow-list configured", is **refuted**: measured 0 of 6 private targets surviving `public_url_or_none` under `allowed_hosts=("example.com",)`, and `filter_js_endpoints` dropped all 6. The chain `public_url_or_none` -> `safe_public_url_or_none` -> `normalize_public_http_url:180` applies the refusal, and the `app/models/address_class.py` extraction (881bdc7) is what closed it. Bare hostnames (`metadata.google.internal`) deliberately pass `public_url_or_none` — a name cannot be classified without DNS, and that refusal belongs to the pin layer (A4, gated). What remains true is that the egress doors are correct **by discipline only**: no gate would notice a new door added without the refusal |
| A3c | A URL the service DISCLOSES has been through the same policy as a URL it FETCHES | frame / form-action / feed extraction | signature | none — these accept `host_policy` and never use it, and the values reach the wire | `UNGATED` — frames, form actions and feed URLs were emitted unvalidated, including private targets |
| A4 | `build_pinned_request` always pins the resolved address; an unpinned plain-HTTP request is unrepresentable — there is no pin switch to leave off, and the refusal surfaces as an ordinary `ValueError` | `app/transport/pinned_transport.py:build_pinned_request` | runtime construction | `tests/crawl/test_pinned_transport.py` (composition contract: pinned IP + Host header, SNI/hostname, IPv6 bracketing) + `tests/crawl/test_ssrf.py` (unresolvable-host refusal reaches the caller as a `ValueError`) | `GATED` |
| A5 | Robots rules are fetched, parsed, cached and served by one owner, with one cache, one TTL policy, one failure semantic and per-origin single-flight | `app/orchestration/robots.py:load_robots_rules` | runtime construction | property registry `robots-policy` + consolidation tests | `GATED`, weak seam (see W1) |
| A6 | A domain's politeness floor may only be installed through the scheduler setter, which clamps it to the service ceiling | `app/orchestration/scheduler.py:set_domain_min_delay` | runtime construction | `test_domain_politeness_floor_is_committed_only_via_the_setter` (AST) | `GATED` |
| A7 | Every redirect hop re-validates through the value's own `resolve` rather than composing a new target; a refused hop raises instead of being read as the page | `app/transport/pinned_transport.py:_redirect_target` | runtime construction | `tests/crawl/test_redirect_target.py` (per-hop decisions, origin-boundary header scrub) + `tests/crawl/test_http_redirect_ssrf.py` (unsafe hop refused before any second request) | `GATED` |
| A8 | Caller credentials never cross a host boundary, and the six header names that would defeat SSRF or spoof a client address are refused at validation | `app/admission/credentials.py`, `app/models/crawl_policy.py` policy validator | runtime construction | policy-validator tests; hop scrub | `GATED` |

## B. Bounds and bounded computation

| # | Property | Owner | Seam | Gate | Status |
|---|---|---|---|---|---|
| B1 | Attacker-influenced regex is compiled and executed through the bounded primitive with a native timeout, never through bare `re` | `app/safety/safe_regex.py` | runtime construction | dynamic-pattern + direct-import + bare-compile AST detectors; timing-contract tests | `GATED` |
| B2 | Robots wildcard matching is linear-time in the input, not backtracking | `app/orchestration/robots_parse.py` (line scanner) | implementation | timing-contract tests | `GATED` |
| B3 | Decompression is bounded **and fails closed**: no branch may expand the body before the cap is checked | `app/transport/decompression.py` | runtime construction | AST requires a cap at every call site; behavioral bomb tests | `PARTIAL` — the no-streaming-API fallback expands before rejecting |
| B4 | Response bodies are read only through the bounded reader; no bare `.read()` / `.content` on an unbounded path | `app/safety/bounded_io.py` | AST route | bare-body-read detector | `GATED` |
| B5 | Caller policy bounds (page count, depth, rps, content length, volume) are **rejected** when out of range, never silently clamped into a different contract | `app/models/crawl_policy.py` + `app/admission/payload_helpers.py:payload_policy` | runtime construction | validation tests for each field | `GATED` |
| B6 | Sitemap `lastmod` cannot place a seed in the future beyond the configured skew — future-dated freshness always re-fetches | `app/orchestration/engine.py:_parse_lastmod` | runtime construction | sitemap tests | `GATED` |
| B7 | Browser screenshot output is bounded in both count and bytes | `app/transport/browser.py` | — | none | `UNGATED` |
| B8 | A browser action chain has a total budget and a rejected action count, not only a per-action ceiling | `app/transport/browser.py:_run_action_chain` | runtime construction | `test_the_action_chain_is_always_invoked_with_a_budget` (AST, tree-wide) + paired bites; budget-exhaustion behavior test | `PARTIAL` — the total budget is gated now and its exhaustion is logged with the URL; the rejected-action COUNT is still not produced (each swallowed failure is logged on its own, so 50 failed actions leave 50 lines and no tally) |
| B9 | Profile knobs have ceilings, not floors only | browser profile parsers | — | floor-only today | `PARTIAL` |
| B10 | A durable job's results do not grow one SQLite row without bound | `app/storage/job_store.py` | — | none (known unsolved; the growth route is a separate results store) | `UNGATED` |

## C. Result and wire contract

| # | Property | Owner | Seam | Gate | Status |
|---|---|---|---|---|---|
| C1 | Every result's `classification`, `strategy`, `error` and `status` is a member of its declared type at the moment of construction | `app/models/literal_checked.py:check_literal_fields` | **runtime construction** | inherited `__post_init__` from `app/models/literal_checked.py:LiteralChecked` — every taxonomy dataclass inherits it, so a result dataclass added tomorrow is checked on arrival rather than by remembering to add a trampoline + membership-gate AST detector + taxonomy drift tests | `GATED` — the strongest gate in the repo |
| C1b | A validated input is resolved exactly once and the resolved value is what everything downstream sees | `app/transport/request_profile.py` | runtime construction | none — a stateful Mapping is iterated twice (snapshot for diagnostics, re-validated for headers), so headers and diagnostics can disagree | `UNGATED` |
| C2 | Terminal and failure result shapes are built by the shared builders, never restated per path | `app/orchestration/engine_result.py` + `app/orchestration/render_result.py` | runtime construction | construction-site AST detectors (primitive set is hand-maintained) | `GATED`, registry of primitives is hand-maintained |
| C3 | A terminal result's status agrees with the status its classification maps to | `app/orchestration/engine_result.py:_enforce_status_classification` | runtime construction | completeness test pinning every enum member | `GATED` |
| C4 | `None` strategy means "no strategy was attempted" — no path invents a strategy label | `app/orchestration/render_result.py` + `app/orchestration/engine_result.py` | runtime construction | strategy-value detector + construction gate | `GATED` |
| C5 | The unified crawl response is constructed only by the response shaper | `app/api/response_shaping.py` | runtime construction | AST construction detector | `GATED` |
| C6 | Wire-visible payloads are model-derived: a field that is not on the response model cannot reach the wire, and MCP input schemas are derived from the same models the handlers use | `app/discovery/*`, `app/mcp/tools.py` | config | endpoint-level contract tests | `PARTIAL` — MCP **input** schemas are still hand-written dicts |
| C7 | Diagnostics report what actually happened: a check that was skipped, served from cache, or never performed is distinguishable from one that was evaluated | `app/transport/fetch_policy.py:_discovery_diagnostics`, engine call sites | runtime construction | metadata tests | `PARTIAL` — a second call site defaulted the flag and claimed an evaluation (being fixed) |

## D. Durable state and the control plane

| # | Property | Owner | Seam | Gate | Status |
|---|---|---|---|---|---|
| D1 | Job status changes only through a compare-and-set transition; a data-plane save never writes status | `app/storage/job_store.py:set_status` | runtime construction | control-plane mutation AST detector + store tests | `GATED` |
| D2 | Schema changes only through the migration ledger, and a migrated database matches the declared schema | `app/storage/migrations/` | AST route + runtime | DDL confinement detector + fresh/legacy/drift tests | `PARTIAL` — the drift helper has NO production caller: `migrate_store` returns early when nothing is pending and never verifies, so an out-of-band change is accepted at startup and fails later at query time. The tests proved the helper, not the seam |
| D3 | SQLite durability configuration is set in one place | `app/storage/sqlite_store.py:connect_store` | config | `test_store_pragma_chokepoint` | `GATED` |
| D4 | Persisted collections hold canonical identity, and a legacy row is normalized once at the storage boundary | `app/storage/job_store.py:from_json` | runtime construction | store tests incl. legacy row shape | `GATED` |
| D5 | Job and result persistence never blocks the event loop: the store owns the offload, so no async frame can reach a blocking store call at all | `app/storage/job_store.py`, `app/storage/result_store.py` (every public method has an `a`-twin) | runtime construction | `test_no_sync_store_method_is_called_from_an_async_frame` (AST, tree-wide) + paired bites; four loop-guard tests that drive real store operations on a loop and fail if one runs on it | `GATED` — was per-call-site `to_thread` wrappers, which is the shape the next method added without one; the gate is now the seam |
| D6 | A job's frontier is never mutated outside the admission chokepoint | `app/admission/admission.py` | AST route | frontier-growth detector | `GATED` |
| D7 | Recursive admission has exactly one owner for identity, filters, robots, delay and the cap predicate | `app/admission/admission.py` | runtime construction | payload-confinement + filter re-inline detectors | `GATED` — new this cycle |
| D8 | A result-callback failure is never reported as a work failure, and a raising sink is always logged | `app/orchestration/scheduler.py:DomainScheduler.run` (the `deliver` closure) | runtime construction | delivery-is-outside-the-worker-try drift test (AST, tree-wide) + paired bites; a behavior test driving a raising sink | `GATED` — new this cycle |

## E. Resources, concurrency and the event loop

| # | Property | Owner | Seam | Gate | Status |
|---|---|---|---|---|---|
| E1 | Blocking I/O never runs on the event loop | the store family owns it (D5); other callers | AST route + spy | await-inside-lock detector; `test_no_sync_store_method_is_called_from_an_async_frame` | `PARTIAL` — the two SQLite stores are now gated at the seam rather than per call site, but the property is wider than that family and the remaining blocking calls (decompress, file writes) are not swept tree-wide |
| E2 | No lock is held across blocking disk I/O | `app/transport/content_cache.py` | runtime construction | AST detector + race tests | `GATED` |
| E3 | Loop-bound primitives are rebound when the loop changes | `app/transport/http_client_pool.py:_LoopBoundPooledClient`, async runner semaphore | runtime construction | loop-bound detector | `GATED` |
| E4 | Remote politeness directives cannot install an unbounded sleep | `app/orchestration/scheduler.py` | runtime construction | clamp tests | `GATED` |
| E5 | Work submitted to a closing loop cannot raise into a completed task | `app/transport/http_client_pool.py` close helpers | runtime construction | deterministic close-race tests | `GATED` |
| E6 | A worker handed to the pipeline is a coroutine function or a plain function returning a result — never a plain function returning a coroutine | `app/orchestration/worker.py:call_worker` | runtime construction | the seam raises `TypeError` naming the worker; `test_a_sync_worker_returning_a_coroutine_is_named_at_the_seam` | `GATED` — new this cycle; before it, the mistake surfaced ~100 lines downstream as a browser `network_error` |
| E7 | A pooled browser slot is held for one deadline, not one per action | `app/transport/browser.py:_run_action_chain` | runtime construction | `test_the_action_chain_is_always_invoked_with_a_budget` (AST, tree-wide) + paired bites; budget-exhaustion behavior test | `GATED` — new this cycle; the deadline is an optional argument so a test can drive the raw executor, and the sweep is what keeps production honest |

## F. Configuration ownership

| # | Property | Owner | Seam | Gate | Status |
|---|---|---|---|---|---|
| F1 | An environment boolean has one spelling policy, in either polarity, wherever it is read | `app/safety/validation.py:parse_bool_value` | runtime construction | property registry `bool-env` + decision-path tests | `PARTIAL` — `CRAWLUTRON_LOOP_LAG_PROBE` is still parsed with `== "1"` in `app/runtime/resource_pressure.py:45`, so `" true "` and `"yes"` read false there and true in the shared reader; two test-only parsers in `app/extraction/content.py` do the same |
| F2 | Every documented `CRAWLUTRON_*` knob has a real production consumer, and env names resolve through declared aliases | `app/config/settings.py` | runtime construction | documented-knob detector | `PARTIAL` — a bare mention was accepted as a consumer (being fixed) |
| F3 | Two documented knobs cannot silently collapse onto one settings field | `app/config/settings.py:ENVIRONMENT_FIELD_ALIASES` | runtime construction | alias integrity check | `PARTIAL` — duplicate targets were accepted (being fixed) |
| F4 | Numeric settings are read through never-raise readers that warn and fall back; there is one int face and one float face | `app/safety/validation.py:_mapping_number` | runtime construction | differential tests over bounds, garbage, non-finite | `GATED` |
| F5 | The process bind port is owned by the process invocation, not duplicated into application settings | Dockerfile / compose | config | documented-knob detector (field removed) | `GATED` |
| F6 | A settings default that DEPENDS on the machine is derived when the settings are constructed, never evaluated once at import | `app/config/settings.py` (the two httpx pool fields) | runtime construction | `test_the_pool_default_is_derived_per_construction_not_frozen_at_import` — patches the memory read and asserts a freshly built instance follows it | `GATED` — new this cycle; a call in the class body froze the cgroup read (and its 4 GiB failure fallback) for the process lifetime, and nothing in the config could explain the resulting number |

## G. Process level

| # | Property | Owner | Seam | Gate | Status |
|---|---|---|---|---|---|
| G1 | The service sends no telemetry, and no dependency may introduce one | dependency configuration | config | `docs/references/crawlutron-telemetry.md` documents the posture; no mechanical gate | `PARTIAL` — documentation, not enforcement |
| G2 | Custom safety predicates have exactly one owner and one exit path; a standard linter must not reimplement them | `tests/invariants/` | config | architecture convention; registry integrity for properties | `PARTIAL` — not mechanically enforced |
| G3 | No inline suppression of the style gate | ruff config | config | `RUF100` is selected, so a `# noqa` that suppresses nothing is a lint error; the tree holds zero `# noqa` comments | `ENFORCED` |

---

## Weak seams

The recurring failure is a gate expressed as a **call shape** when the property
is about **value or ownership**. Each of these was demonstrated, not suspected:

- **W1 — helper-scoped extraction.** All three registered properties are
  detected by fingerprints evaluated within a function body. Moving the second
  implementation into a helper defeats all three. The property is "this
  capability has one implementation anywhere in `app/`"; the gate asks "does this
  function body contain this call shape". **Confirmed by an evasion matrix: zero
  offenders for bool normalization split into a helper or testing membership via
  `dict`/`.keys`, public-address logic split into a helper or reached through
  dynamic `getattr`, a robots parser split into a helper or built by an
  arbitrary parser returning a dict.**
- **W2 — constructed strings.** The robots-URL fingerprint matches a contiguous
  `robots.txt` literal; a URL assembled with `.format`, `str.join`, or an
  f-string is missed. Confirmed.
- **W3 — configuration self-editing.** Deleting a fingerprint from the property
  registry previously left every test green. Now closed: required fingerprints
  are hardcoded and each must be observed in the tree, so dead configuration
  fails.
- **W4 — reference counted as use.** The documented-knob detector accepted a
  docstring mention as a production consumer. A consumer must be a resolved
  read, not a reference.
- **W5 — a required argument mistaken for enforcement.** A signature detector
  proves the argument is *present*, not that the function *uses* it. A class
  annotated with the policy type exempts every method, including ones that never
  read it, and a resolved-name pattern misses a real dotted import
  (`urllib.parse.urljoin`) while flagging nothing about the two functions that
  actually ignore the policy. Key on the property — does this function return or
  forward a URL without the policy in scope — and prove it with the producer and
  the consumer in DIFFERENT modules, because a proof that puts both in one
  synthetic module cannot see a cross-module split.
- **W6 — resolution scoped to one file.** The property detectors resolve a helper
  only within the same module, so splitting an implementation across two modules
  evades all three. The proofs cannot see it because both halves live in one
  synthetic module.

**The family lesson.** Fixing the next instance individually is a treadmill.
When a gate is evaded, the fix belongs in the shared matching machinery, and
the audit question is which other properties share the weakness — W1 and W2
apply to every registered property simultaneously.

## Priority list

Ordered by what a violation costs. This is the budget the remaining waves spend.

1. **A3 host policy cannot be omitted** — a confirmed bypass: a crawl with an
   allow-list fetched a blocked host through the discovery path. Security.
   *Gate: a signature detector that fails if any URL-normalizing entry point
   makes the policy optional.*
2. **A3b a URL path that applies no SSRF policy at all** — `public_url_or_none`
   and the JS-endpoint filter accept private URLs even when the policy would
   refuse them, and take no policy argument. This is the same defect class as A3
   in a second location, and it predates this cycle. Security. *Gate: one rule —
   no URL-returning helper may lack the policy — plus a private-URL case per
   entry point.*
2. **B10 unbounded job result growth** — a whole-site job's results grow one row
   and one in-memory list forever. Silent resource exhaustion. *Gate: none
   feasible without the results-store design; treat as a product decision with a
   documented bound.*
3. **B3 decompression fails closed** — one branch expands before the cap is
   checked. Memory exhaustion under a hostile response. *Gate: a detector on the
   owner's internal dataflow, not only on its call sites.*
4. **C6 wire schemas are model-derived** — MCP input schemas are hand-written
   and have already drifted from the models the handlers use. *Gate: direct
   schema equality against the models.*
5. **B7 / B8 browser output bounds** — B8's total budget is now gated (this
   cycle: one deadline per chain, tree-wide enforced); what remains is the
   rejected-action COUNT and B7's unbounded screenshots. Resource exhaustion.
   *Gate: behavioral caps with a count and byte ceiling.*
6. **E1 blocking I/O on the loop** — the two SQLite stores now own their
   offload and a tree-wide AST rule fails on any async frame reaching a
   blocking store call (this cycle), but the property is wider than that
   family: decompress and file writes are still unswept. *Gate: the same AST
   rule widened to the remaining known blocking callees.*
7. **G1 telemetry** — currently a document. *Gate: a dependency-allowlist check
   at install time.*
8. **W1 / W2 property fingerprints** — every registered property shares the
   weakness. *Fix in the shared matcher, once, family-wide.*
9. **C7 diagnostics honesty** — being fixed this wave; verify no other call site
   defaults an evaluation flag.
10. **F2 / F3 configuration ownership** — being fixed this wave.

## What this file owes the next wave

A property moves off `UNGATED` when a gate exists **and** a proof exists that
the gate fails when the property is violated. A property is not "covered"
because a reviewer believes the code is correct. When a wave closes an entry, it
moves the row and names the gate — in the same commit, so the inventory cannot
drift from the code it describes.
