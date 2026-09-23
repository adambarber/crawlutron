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
| A1 | Canonical page identity is computed by one function, and every frontier key, visited entry, cache key and fetch target derives from it — never from a raw submitted string | `app/validation.py:normalize_public_http_url`, `app/crawl/admission.py:canonical_page_identity` | runtime construction | `test_batch_dedup_keys_on_canonical_page_identity` (AST, tree-wide); job-store normalization tests | `GATED` |
| A2 | The public-address verdict (private / loopback / link-local / reserved / CGNAT / NAT64 / ULA / multicast) has exactly one implementation | `app/validation.py:_is_public_ip` | runtime construction | property registry `public-target` + reserved-range corpus test + bite proof | `GATED`, weak seam (see W1) |
| A3 | The host allow/deny policy cannot be omitted by any path that normalizes or admits a URL | `app/validation.py:_host_policy_reason` | **signature (required argument)** | being added this wave; a seed-list-only enforcement was found and is being removed | `PARTIAL` → in flight |
| A3b | **Every** URL entry point applies the private-target refusal, not only the fetch path | `app/validation.py` (`public_url_or_none`, `filter_js_endpoints`) | signature | none — confirmed to pass private URLs even with an allow-list configured | `UNGATED` — a second, pre-existing SSRF path |
| A4 | `SafeUrl.parse` either pins the resolved address or fails closed; it never delivers to an unpinned host on HTTPS | `app/safe_url.py:48` | runtime construction | `test_safe_url_parse_must_pin` (AST) | `GATED` |
| A5 | Robots rules are fetched, parsed, cached and served by one owner, with one cache, one TTL policy, one failure semantic and per-origin single-flight | `app/crawl/robots.py:load_robots_rules` | runtime construction | property registry `robots-policy` + consolidation tests | `GATED`, weak seam (see W1) |
| A6 | A domain's politeness floor may only be installed through the scheduler setter, which clamps it to the service ceiling | `app/scheduler.py:set_domain_min_delay` | runtime construction | `test_domain_politeness_floor_is_committed_only_via_the_setter` (AST) | `GATED` |
| A7 | Every redirect hop re-validates through the URL chokepoint rather than composing a new target | `app/safe_url.py:redirect_hop` | AST route | redirect-hop shape detector (tree-wide) | `GATED` |
| A8 | Caller credentials never cross a host boundary, and the six header names that would defeat SSRF or spoof a client address are refused at validation | `app/crawl/credentials.py`, `app/api_models.py` policy validator | runtime construction | policy-validator tests; hop scrub | `GATED` |

## B. Bounds and bounded computation

| # | Property | Owner | Seam | Gate | Status |
|---|---|---|---|---|---|
| B1 | Attacker-influenced regex is compiled and executed through the bounded primitive with a native timeout, never through bare `re` | `app/safe_regex.py` | runtime construction | dynamic-pattern + direct-import + bare-compile AST detectors; timing-contract tests | `GATED` |
| B2 | Robots wildcard matching is linear-time in the input, not backtracking | `app/crawl/robots_parse.py` (line scanner) | implementation | timing-contract tests | `GATED` |
| B3 | Decompression is bounded **and fails closed**: no branch may expand the body before the cap is checked | `app/crawl/decompression.py` | runtime construction | AST requires a cap at every call site; behavioral bomb tests | `PARTIAL` — the no-streaming-API fallback expands before rejecting |
| B4 | Response bodies are read only through the bounded reader; no bare `.read()` / `.content` on an unbounded path | `app/bounded_io.py` | AST route | bare-body-read detector | `GATED` |
| B5 | Caller policy bounds (page count, depth, rps, content length, volume) are **rejected** when out of range, never silently clamped into a different contract | `app/api_models.py` + `app/crawl/payload_helpers.py:_payload_policy` | runtime construction | validation tests for each field | `GATED` |
| B6 | Sitemap `lastmod` cannot place a seed in the future beyond the configured skew — future-dated freshness always re-fetches | `app/crawl/engine.py:_parse_lastmod` | runtime construction | sitemap tests | `GATED` |
| B7 | Browser screenshot output is bounded in both count and bytes | `app/crawl/strategies/browser.py` | — | none | `UNGATED` |
| B8 | A browser action chain has a total budget and a rejected action count, not only a per-action ceiling | `app/crawl/strategies/browser.py` | — | none | `UNGATED` |
| B9 | Profile knobs have ceilings, not floors only | browser profile parsers | — | floor-only today | `PARTIAL` |
| B10 | A durable job's results do not grow one SQLite row without bound | `app/crawl/job_store.py` | — | none (known unsolved; the growth route is a separate results store) | `UNGATED` |

## C. Result and wire contract

| # | Property | Owner | Seam | Gate | Status |
|---|---|---|---|---|---|
| C1 | Every result's `classification`, `strategy`, `error` and `status` is a member of its declared type at the moment of construction | `app/models.py:_check_literal_fields` | **runtime construction** | `__post_init__` on all three result dataclasses + membership-gate AST detector + taxonomy drift tests | `GATED` — the strongest gate in the repo |
| C2 | Terminal and failure result shapes are built by the shared builders, never restated per path | `app/crawl/result_helpers.py` | runtime construction | construction-site AST detectors (primitive set is hand-maintained) | `GATED`, registry of primitives is hand-maintained |
| C3 | A terminal result's status agrees with the status its classification maps to | `app/crawl/result_helpers.py:_enforce_status_classification` | runtime construction | completeness test pinning every enum member | `GATED` |
| C4 | `None` strategy means "no strategy was attempted" — no path invents a strategy label | `app/crawl/result_helpers.py` | runtime construction | strategy-value detector + construction gate | `GATED` |
| C5 | The unified crawl response is constructed only by the response shaper | `app/api/response_shaping.py` | runtime construction | AST construction detector | `GATED` |
| C6 | Wire-visible payloads are model-derived: a field that is not on the response model cannot reach the wire, and MCP input schemas are derived from the same models the handlers use | `app/discovery/*`, `app/mcp/tools.py` | config | endpoint-level contract tests | `PARTIAL` — MCP **input** schemas are still hand-written dicts |
| C7 | Diagnostics report what actually happened: a check that was skipped, served from cache, or never performed is distinguishable from one that was evaluated | `app/crawl/strategies/fetch_policy.py:_discovery_diagnostics`, engine call sites | runtime construction | metadata tests | `PARTIAL` — a second call site defaulted the flag and claimed an evaluation (being fixed) |

## D. Durable state and the control plane

| # | Property | Owner | Seam | Gate | Status |
|---|---|---|---|---|---|
| D1 | Job status changes only through a compare-and-set transition; a data-plane save never writes status | `app/crawl/job_store.py:set_status` | runtime construction | control-plane mutation AST detector + store tests | `GATED` |
| D2 | Schema changes only through the migration ledger, and a migrated database matches the declared schema | `app/crawl/migrations/` | AST route + runtime | DDL confinement detector + fresh/legacy/drift tests | `GATED` |
| D3 | SQLite durability configuration is set in one place | `app/crawl/sqlite_store.py:connect_store` | config | `test_store_pragma_chokepoint` | `GATED` |
| D4 | Persisted collections hold canonical identity, and a legacy row is normalized once at the storage boundary | `app/crawl/job_store.py:from_json` | runtime construction | store tests incl. legacy row shape | `GATED` |
| D5 | Job persistence does not block the event loop | `app/api/job_runner.py` (store `asave`) | runtime construction | off-loop save tests | `GATED` |
| D6 | A job's frontier is never mutated outside the admission chokepoint | `app/crawl/admission.py` | AST route | frontier-growth detector | `GATED` |
| D7 | Recursive admission has exactly one owner for identity, filters, robots, delay and the cap predicate | `app/crawl/admission.py` | runtime construction | payload-confinement + filter re-inline detectors | `GATED` — new this cycle |

## E. Resources, concurrency and the event loop

| # | Property | Owner | Seam | Gate | Status |
|---|---|---|---|---|---|
| E1 | Blocking I/O never runs on the event loop | call sites | AST route + spy | await-inside-lock detector; `asyncio.to_thread` spy in tests | `PARTIAL` — the spy records but does not enforce; coverage is per-call-site |
| E2 | No lock is held across blocking disk I/O | `app/crawl/content_cache.py` | runtime construction | AST detector + race tests | `GATED` |
| E3 | Loop-bound primitives are rebound when the loop changes | `app/crawl/strategies/http_client_pool.py:_LoopBoundPooledClient`, async runner semaphore | runtime construction | loop-bound detector | `GATED` |
| E4 | Remote politeness directives cannot install an unbounded sleep | `app/scheduler.py` | runtime construction | clamp tests | `GATED` |
| E5 | Work submitted to a closing loop cannot raise into a completed task | `app/crawl/strategies/http.py` close helpers | runtime construction | deterministic close-race tests | `GATED` |

## F. Configuration ownership

| # | Property | Owner | Seam | Gate | Status |
|---|---|---|---|---|---|
| F1 | An environment boolean has one spelling policy, in either polarity, wherever it is read | `app/validation.py:parse_bool_value` | runtime construction | property registry `bool-env` + decision-path tests | `GATED`, weak seam (see W1) |
| F2 | Every documented `BROWSER_CRAWLER_*` knob has a real production consumer, and env names resolve through declared aliases | `app/settings.py` | runtime construction | documented-knob detector | `PARTIAL` — a bare mention was accepted as a consumer (being fixed) |
| F3 | Two documented knobs cannot silently collapse onto one settings field | `app/settings.py:ENVIRONMENT_FIELD_ALIASES` | runtime construction | alias integrity check | `PARTIAL` — duplicate targets were accepted (being fixed) |
| F4 | Numeric settings are read through never-raise readers that warn and fall back; there is one int face and one float face | `app/validation.py:_mapping_number` | runtime construction | differential tests over bounds, garbage, non-finite | `GATED` |
| F5 | The process bind port is owned by the process invocation, not duplicated into application settings | Dockerfile / compose | config | documented-knob detector (field removed) | `GATED` |

## G. Process level

| # | Property | Owner | Seam | Gate | Status |
|---|---|---|---|---|---|
| G1 | The service sends no telemetry, and no dependency may introduce one | dependency configuration | config | `docs/references/browser-crawler-telemetry.md` documents the posture; no mechanical gate | `PARTIAL` — documentation, not enforcement |
| G2 | Custom safety predicates have exactly one owner and one exit path; a standard linter must not reimplement them | `tests/test_safety_invariants.py` | config | architecture convention; registry integrity for properties | `PARTIAL` — not mechanically enforced |
| G3 | No inline suppression of the style gate | ruff config | config | ruff config; the "no noqa" rule is convention | `PARTIAL` |

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
5. **B7 / B8 browser output bounds** — unbounded screenshots and an unbounded
   action chain. Resource exhaustion. *Gate: behavioral caps with a count and
   byte ceiling.*
6. **E1 blocking I/O on the loop** — enforcement is a spy, not a gate, and
   coverage is per call site. *Gate: an AST rule for known blocking callees
   outside an offload helper.*
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
