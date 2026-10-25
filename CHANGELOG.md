# Changelog

All notable changes to this project are documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Security
- **Webhook doors refuse plaintext `http://` (#499).** `webhook_url` was accepted
  at both doors -- the crawl 202 path and watch registration -- with any
  `http://` scheme: the HMAC signature scheme protects integrity, not
  confidentiality, so the signature AND the full crawl payload (possibly
  including extracted page text) rode to a plaintext callback unencrypted. One
  law now governs both doors (`ensure_https_webhook_url` in `webhooks.py`):
  `https://` is required unless the operator opts out with
  `CRAWLUTRON_ALLOW_PLAINTEXT_WEBHOOK=1`, mirroring the
  `CRAWLUTRON_ALLOW_PRIVATE_TARGETS` escape-hatch shape (value-keyed memo,
  default fail-closed, one warning per distinct env value). Refused at
  admission with the existing 422 `validation_error` envelope in
  `tests/test_webhook_ssrf.py` (unit) and both door suites. `webhook_url` never
  fired at a target the caller chose less than crawl targets do, so the trust
  position is identical and both fail closed.
- **`/v1/keys` is operator-only; a caller key no longer mints or revokes keys
  (#436).** `get_keys_caller` fell through to ordinary verification whenever
  the presented secret was not the operator token, so ANY live caller key
  could mint fresh unbounded identities (`max_pages=0, max_concurrency=0`
  default to unbounded), revoke any other tenant's key by id, and read every
  tenant's labels and quotas -- a cross-tenant authz break one POST away.
  The door now accepts only the configured operator token
  (`CRAWLUTRON_KEY_ADMIN_TOKEN`) and fails closed when none is configured,
  which is the contract `.env.example` and `CrawlerSettings` already
  documented ("empty disables issuance/listing/revocation over HTTP") and
  `scripts/e2e_verify.py` already assumed. The refusal is the door's existing
  401 `invalid_api_key` envelope -- no store lookup, so the keys door is not
  a key-validity oracle, and callers learn nothing about which keys are live.
  Refusals are pinned in `tests/api/test_caller_auth.py`: a tenant key cannot
  mint, list, or revoke, and the sealed door stays sealed without a token.
- **Render options are ceilinged: an anonymous caller can no longer park a
  pooled renderer (#444).** `resolve_render_options` accepted
  `options.timeout_seconds=1000000000` and any `max_retries`, so one
  `/v1/render` request held one of a handful of pooled renderers (~700 MiB
  apiece) for as long as the target stalled, retried indefinitely -- while
  the crawl path bounded the identical knobs through `CrawlPolicyLimits` all
  along. The caller-supplied knobs now clamp at the tree's own constants
  (`MAX_HTTP_TIMEOUT_SECONDS`=120, `MAX_RETRIES`=5), silently, so sane
  requests behave identically; the clamp binds the caller's side only,
  because the resolver is the shared chokepoint and `_browser_options` feeds
  it policy limits whose per-job retry ceiling (`MAX_JOB_MAX_RETRIES`=10) is
  a documented contract. Profile strings -- an action `selector`, a
  `storage_state`, a header-like `user_agent`/`accept` value riding the
  raw-dict arm -- were equally unsized and are now refused at #441's
  `MAX_HEADER_VALUE_BYTES` (8 KiB) at both the boundary (a 422 naming the
  key) and the resolver (the render edge's own refusal), the same
  boundary+reader pair the profile knob ceilings already use. The
  `request_profile` pool entries (`user_agents`, `accept_languages`) keep
  their entry-count ceiling and remain #454's surface.
- **Result-store payloads are bounded and the policy rollup leaves the event
  loop (#443, #449).** `CrawlResultStore.put` accepted payloads of any size,
  so one oversized crawl result was retained forever and re-read in full on
  every listing; `put` now refuses past the byte cap *before* the INSERT
  (the row is never stored -- `get` returns None, both `aput` callers catch
  and log `crawl_result_store_failed`, the crawl response still reaches the
  caller). The unbounded `payloads()` fetchall is deleted; `/v1/policy-
  suggestions` read every retained row on the event loop -- validating each
  trace inline -- so a large store stalled the whole service. The rollup now
  pages by keyset cursor (`payloads_page`, tie-safe on `(stored_at,
  request_id)`), validates and rolls up via `asyncio.to_thread`, and reports
  an honest window (`records_examined` = valid + malformed + unread; the
  O(n^2) signature dedupe is a capped first-seen set).

### Changed
- **An app class may no longer promise a container interface nobody asked for.**
  `_LockedDomainCookieStore` subclassed `MutableMapping[str, str]`, and ten of
  its thirteen methods existed only to honour that promise: `__getitem__`,
  `__setitem__`, `__delitem__`, `__iter__`, `__len__`, `__contains__`, `pop`,
  `update`, `__repr__`, `clear`. No production path reached any of them —
  production went through `store`, `touch_get`, `clear` and `len`, and that is
  all the class now declares. The surface existed so four test files could seed
  and restore a process global, which is a scaffold, not a contract.
  `no-unclaimed-container-protocol` holds the line: an `app/` class may inherit
  a container protocol only when an app-side signature names it in a type
  position. Mention is not enough — the offender's own instantiation named it,
  and counting that is exactly what let the ten methods read as claimed. The
  browser renderer's cookie store is now dropped by the `conftest` process-state
  door between tests, the way the cookie jar, the client pool and the robots
  cache already were.

### Added
- **Caller identity is enforced at the read edge: keys API, scoped reads,
  labelled metrics, quotas (#25).** Callers authenticate with an API key
  presented as `X-API-Key` (or `Authorization: Bearer`), and every read edge —
  `GET /v1/crawl/{request_id}`, the jobs surface, labelled metrics — scopes what
  it returns to the caller's identity, so one caller cannot read another's jobs
  or results. `/v1/keys` issues, lists and revokes caller keys; issuance is
  operator-gated via `CRAWLUTRON_KEY_ADMIN_TOKEN`. Keyless requests crawl as a
  shared anonymous identity with bounded default quotas. Isolation is pinned in
  `tests/api/test_caller_isolation.py` and the refusal matrix in
  `tests/api/test_caller_auth.py`.
- **A page's durable identity is one function, and a gate holds it there.**
  `visited`, `in_flight` and `frontier` all key on page identity, so they have
  to agree — but "canonical form, else the raw URL" was written out at twelve
  call sites across five modules, and in three spellings. Some recomputed what
  the job store had already memoized, and that memoization exists because
  re-deriving identity for every stored URL on every read cost ~0.23s on a
  20k-entry job. `durable_page_identity` now owns the rule;
  `tests/invariants/test_page_identity.py` fails on a thirteenth call site
  written in a fourth way. Two notes on how the gate earns that: it watches all
  three names the canonicalization answers to, not just the alias the first
  draft matched, and it exempts the owner's own body by function rather than by
  module — otherwise it would have reported clean while `extraction.py` and
  `request_pipeline.py` wrote the same rule against the root function. The owner
  returns `str` rather than `NormalizedUrl` on purpose: when a URL has no
  canonical form the raw value comes back unchanged, and the old `... or url`
  spelling made a type claim the function could not keep, eleven times over,
  implicitly.
- **Address classification is now one primitive, and the service says when it
  is reachable from the network.** Parsing a host into an address, unwrapping
  any IPv4 inside an IPv6 wrapper, and reading the range predicates was written
  out at each call site, so a range added for one consumer was invisible to the
  others. `app/addresses.py` owns that work as `classify()`, and the egress
  guard (`app/validation.py`), the pinning path, and the new startup bind check
  all ask that one classification. The two *questions* stay separate on
  purpose: `10.0.0.5` is not a safe public egress target and is still
  reachable by every host on the LAN, so `is_loopback_bind()` is its own
  predicate rather than an inversion of the egress verdict. Because nothing
  authenticates a caller — every `Authorization` header in the tree is
  outbound, attached to a crawled origin rather than presented by one — the
  service now logs a warning at startup when its bind address leaves loopback,
  naming the exposure and how to avoid it. It is a warning and not a refusal
  because the shipped compose file binds `0.0.0.0` inside the container and
  publishes only to `127.0.0.1`, which Docker port mapping requires.
- **A fixed cap on concurrent tracked requests, behind `request_gate` (#299).**
  The service had a resource-saturation guard but no second, absolute ceiling;
  the readiness report already counted active requests, so the new knob rides
  that counter. `CRAWLUTRON_MAX_ACTIVE_REQUESTS` is the knob (0 or unset
  disables it; a positive value bounds concurrent /v1/crawl, /v1/map, /v1/render
  and /v1/search requests, plus the five MCP fetch tools). At the cap the `request_gate` edge refuses with HTTP
  503 and a `capacity_exceeded` envelope — the same 503 the readiness report
  already uses for backpressure — so an externally-configured ceiling sits
  behind the quota system. Optional-presence is explicit: the internal bound
  is `None` when unset/0, and the is-not-None guard is where the refusal
  decision is made.

- **Outbound webhook callbacks are signed, retried, and drained on shutdown.**
  Every delivery is now HMAC-SHA256-signed over the exact body (shared secret via
  the new `CRAWLUTRON_WEBHOOK_SECRET` knob; empty disables signing so existing
  setups are untouched), so a receiver can prove origin and integrity. Transient
  failures (5xx/429/transport) are retried with exponential backoff controlled by
  the new `CRAWLUTRON_WEBHOOK_MAX_ATTEMPTS` and `CRAWLUTRON_WEBHOOK_BACKOFF_BASE_MS`
  knobs instead of being dropped after one attempt. And a runner cancelled at
  shutdown no longer drops the callback whose terminal write already landed: the
  shielded deliveries are drained by `shutdown_job_runner` before the stores close.

### Fixed
- **`/ready` reported `max_active_requests = 0` even when a cap was set.** The
  field hardcoded 0 ("self-managed: no fixed request cap"); with
  `CRAWLUTRON_MAX_ACTIVE_REQUESTS` set, the report now reflects the real
  bound, and 0 only when unrestricted.
- **Doc-pool tests assert the real per-worker contract, not a transient pool snapshot (M1).**
  `test_extract_document_child_death_returns_empty` and `test_extract_document_normal_works`
  in tests/crawl/test_document_workers.py asserted process-global invariants over the shared
  `DOC_POOL` -- no slot busy anywhere, and `pids()` non-empty after a real conversion. A
  natively-crashing worker subprocess under xdist (`libc++abi: recursive_mutex lock failed`)
  can transiently leave a sibling slot busy or empty the pid registry, so the assertions
  failed ~10-17% of load-set runs. The child-death test now isolates the pool it drives
  (`teardown()` first, the same convention `normal_works` already used) so its `health()` loop
  covers only the worker THIS call owns, and `normal_works` asserts no worker is left marked
  busy mid-task instead of a raw pid count. No production behavior changed.
- **A redirect to a different port on the same host kept `Cookie` and `Authorization`.**
  `pinned_transport._redirect_target` decided whether to strip credential headers
  by comparing `urlparse(...).hostname`, so `https://a.com/x` to
  `https://a.com:8443/z` kept all three, and so did an https-to-http downgrade
  on the same host. The service already had the stricter definition in the
  sibling chokepoint: `credentials.origin_of` is `scheme://host[:port]` with
  default ports dropped, and `apply_credentials` sends a credential only to that
  exact origin — so a credential scoped to `https://a.com:443` was already
  refused for `https://a.com:8443`, while one attached to `https://a.com` was
  sent there by the redirect path. Two chokepoints answering "is this the same
  origin?" and disagreeing, with the looser one being where the credential went.
  The scrub now compares `origin_of` on both sides, which does not reconcile the
  two answers but deletes the second one. The default-port case is pinned too,
  because it is the half a naive string comparison gets wrong: every legitimate
  redirect that spells `:443` out would have lost its session.
- **A refused target now gets no identity, instead of its own raw URL back.**
  `app/crawl/admission.py:_frontier_identity` asked the page-identity owner for a
  canonical identity and, when the owner returned `None`, handed back the raw
  string. `None` is how the owner says *refused* -- `private_network_target` or
  `host_not_allowed` -- so the fallback turned a rejection into an answer, and
  every caller could no longer tell the two apart. Its two siblings,
  `_candidate_stream` and `normalize_sitemap_candidates`, both drop on `None`,
  which made this the one place in the module where a refusal and an
  acceptance looked alike. It is now fail-closed like the other two.

  Scope, stated honestly: the admission *decision* was never reached, because
  `_candidate_stream` drops before anything is admitted, and this helper's only
  production caller uses it to build a dedupe reservation set. So this was a
  divergence and a latent hazard, not a live SSRF -- the "denied target is
  persisted and reported as accepted (202)" claim in the property inventory was
  wrong and is corrected there. The hazard was real: a helper whose `None` means
  "refused" but which returns a string anyway is one edit away from being an
  admission.

  Covered by `tests/crawl/test_ssrf.py::TestFrontierIdentityRefusesRejectedTargets`,
  which asserts the refusal for every input shape (string, dict, `FrontierEntry`,
  `(key, entry)` tuple, duck-typed `normalized_url`), for both refusal reasons,
  and -- in the other direction -- that a public target still gets its identity,
  so `return None` cannot satisfy the guard.
- **A job row now belongs to the test that made it.** `app/main.py` binds
  `DATABASE_PATH = settings.cache_dir + "/crawler.db"` at import and hands every
  test one process-wide `JobStore` built on it, and no test overrode
  `CRAWLUTRON_CACHE_DIR`. The whole suite therefore shared a single on-disk
  database: a `PENDING` job row seeded by one test stayed claimable by any other
  test — including one running at the same instant in a different xdist worker,
  since `recover_and_start_pending_jobs` starts every `PENDING` whole-site job it
  finds — and by the next run of the suite. That surfaced as an intermittent
  `assert 'running' == 'pending'` in `test_v1_jobs_create_runs_and_list_get`, and
  it also meant the suite was writing into the developer's real cache directory.
  The suite now points `CRAWLUTRON_CACHE_DIR` at a per-worker scratch directory,
  set in `conftest.py` at import time because import order is the constraint, and
  removed at session teardown — guarded on identity so an operator-supplied
  `CRAWLUTRON_CACHE_DIR` is never deleted.

  This removes cross-*process* sharing only. Roughly 1 run in 10 still flakes,
  because the ASGI tests share the app's process-global runtime — the module
  global `job_store`, the job runner's task registry, and the app's
  `shutdown_event` — so a test that boots a lifespan changes the outcome of
  every other test in its worker. Both the database lock in
  `test_the_lifespan_boots_a_four_worker_fleet` and the stuck-`running` job in
  `test_v1_jobs_create_runs_and_list_get` come from that one channel. See
  issue #35; the direction is a funnel that hands each test an owned view of the
  runtime rather than three separate patches.
- **A test that runs alone must not be the only way a test passes.** Under
  back-to-back suite runs `test_cooldown_delay_applied_to_domain` and
  `test_v1_crawl_async_mode_concurrent_limit` fail, while 20/20 in isolation
  they pass. Both measure elapsed wall-clock instead of an injected clock, so
  they assert on the machine rather than on the code. See issue #36; raising
  the timeout would mask it.
- **`GET /v1/crawl/{request_id}` no longer answers 200 for a job that is not
  finished.** The endpoint read the result store before the job store, so any
  row in the store produced a 200 regardless of the job's status, while its
  docstring promised the opposite. The job is now consulted first; a
  `request_id` with no job at all still answers 200 from the store, which is the
  synchronous path and is finished by construction.
- **A paused or cancelled async crawl no longer publishes results or announces
  completion.** `run_async_crawl` wrote `COMPLETED` through a compare-and-set,
  discarded the result, and then published to the result store unconditionally —
  so a job paused mid-flight still answered 200 to a client polling it and still
  delivered a success webhook for results it would never serve. The set is now
  checked and the publish and webhook ride with it.
- **A crawl whose terminal write loses its compare-and-set is no longer
  stranded.** This is what the discarded result was hiding:
  `recover_orphaned_running_jobs` rewinds `RUNNING` to `PENDING` on startup on
  the assumption that no crawl is in flight, and that assumption does not hold
  when a second app instance shares the store. The job is rewound under a runner
  that is still working, the terminal write loses against `PENDING`, and the job
  is unreachable — `PENDING` is never picked up again, so the client polls a
  completion that cannot arrive. The runner now reconciles: `PAUSED` and
  `CANCELLED` are a caller's decision and are left alone; anything else is a
  control-plane state this crawl did not choose, and results are published
  against the state the job is actually in.
- **`crawl_api` no longer lets a crawl outlive its test.** The fixture handed out
  a client and nothing else, so a test asserting only the 202 left a crawl
  running into the next test's teardown, which replaces the app's process-global
  shutdown event. Teardown now drains the crawls the fixture handed out.

### Fixed
- **A shutdown releases the extraction pools; it no longer retires extraction.**
  `shutdown_doc_extraction_executor` is wired into the app lifespan and called
  `shutdown()` on a module-global executor that was built once at import and
  never rebuilt, so every later selector evaluation raised `cannot schedule new
  futures after shutdown`. Since the shutdown rides on the lifespan, any caller
  that started and stopped the app once killed extraction for the rest of the
  process; in the suite it made the result depend on test ordering, and only a
  randomized run ever exposed it. The sibling `_DOC_POOL` in the same module
  already had the right shape — built on demand, torn down to `None`, rebuilt by
  the next caller — and the thread pool's `if _EXTRACTION_EXECUTOR is not None`
  guard could never be false, because nothing ever assigned `None` to it. Two
  pools in one module disagreed about what shutdown means and only one was safe
  to call twice, so the thread pool now gets the process pool's contract.

### Changed
- **A private name is now gated at the package boundary, and 21 names that
  crossed it were promoted.**
  A leading underscore means package-internal, which is what lets a reader trust
  a name they can see — but nothing enforced it, so any new cross-package
  private import was accepted silently. The rule asks only whether an import
  crosses a package boundary and ignores the name entirely, which keeps it total
  and readable; the 28 sibling reaches *inside* one package are what the
  convention permits and are untouched. Two offenders could not simply lose
  their underscore: `_content_cache_key` lives in `content_cache_key.py` and
  became `build_content_cache_key`, and `_make_engine_result` shares its stem
  with a different test fixture builder and became `build_engine_result`.
- **Every caller-supplied collection on the policy is now bounded, and every caller-supplied key vocabulary is closed.**
  `strategies` was the last uncapped collection on `CrawlPolicy` while its
  siblings all had ceilings (`urls` 500, `headers` 64, `credentials` 32,
  `validators` 64), which reads as an oversight rather than a decision. It is
  the outbound fan-out per URL — `execute_crawl_chain` runs one ladder step per
  entry — so a list of 2,000 was a 200 meaning 2,000 network attempts. Capped
  at 16, and capped rather than deduplicated on purpose: a browser render mints
  a session cookie that a later `HTTP_CFFI_IMPERSONATE` entry replays, so
  `[browser, cffi, cffi]` is a real path and dedupe would have thrown it away.
  A test pins that ladder as accepted so a later reader does not "improve" it
  back into a dedupe.
- **`request_profile` and `browser_profile.actions` are closed vocabularies.**
  `request_profile` was `dict[str, Any]` with no constraints, so
  `{"cookie_jarr": "per_host"}` disabled the per-host jar and reported success,
  and the field description advertised `accept_language` when the consumer reads
  `accept_languages` — prose committing to a key that did nothing. Inside an
  action, `{"type": "fill", "selector": "#q", "val": "hi"}` filled an empty string
  and reported success. Both are the same failure as the inert `state` key this
  series removed from `browser_profile`, at the two places it could still hide.
  Each declared set is asserted against the consumer's own reads, so a reader
  added without a declaration fails where it is written.
- **A closed key set is now also ceilinged.** Closing `browser_profile` in
  #55 said which keys exist and was silent about how large their values could
  be, so four knobs took whatever the caller sent: `lazy_scroll_steps: 10**12`
  on a scroll that runs directly below an action chain whose docstring says
  production must pass a budget; an unbounded cookie-harvest timeout holding a
  *pooled* renderer from a `finally` block; and `screenshot_selectors`, where
  each entry appends a base64 PNG to the result. Each is now bounded at both the
  boundary (a 422 naming the key) and the reader (the actual guarantee, since by
  the time a profile reaches a renderer the validator is no longer in the path).
- **The duplication gate is now 3 lines / 30 tokens, and reports zero clones.**
  The hook read `--min-lines 4 --min-tokens 25`, but jscpd counts *newlines*
  inside a fragment, so a floor of 4 enforced fragments of five lines and was
  blind to every four-line duplicate. At the real 4/25 it already passed while
  17 unsanctioned pairs existed. Dropping to 3/30 surfaced all 17, and each was
  removed by structural change rather than by renaming or reformatting:
  - `app/search/providers.py` — the three keyed providers each re-spelled the
    request skeleton (method, endpoint, pooled call) around a different payload
    lambda. `_ApiKeyProvider` now owns that skeleton; a provider declares a
    `_ProviderCall` and an `_items_path` and nothing else. The invariant that
    guards it was rewritten to the stronger rule: a provider may not define
    `search`, `_request`, `_results` or `_items_of` at all.
  - `app/crawl/recursive_admission.py` — the recursive run's state was declared
    twice, as a concrete dataclass beside the dispatch loop and as a `Protocol`
    beside the stages, with 13 overlapping fields to keep in step by hand. It is
    now declared once, `RecursiveRunState`, in the module that consumes it.
  - `app/resource_pressure.py` — the module docstring restated the throttling
    mechanism it extends instead of pointing at the module that owns it.
  - Test-side, the missing funnels were extracted rather than the pairs
    tolerated: `function_defs`, `enclosing_function` and
    `offenders_over_functions` in `tests/chokepoint_walks.py`;
    `_property_sites` and `_PropertyObservations` in
    `tests/invariants/test_registry.py`; `FieldMembers` in
    `tests/membership_gate.py`. `jscpd-expectations.json` stays empty, which
    `tests/test_jscpd_gate.py` asserts: a sanction is a standing licence to
    duplicate, so every pair gets a chokepoint instead.
- **The tool no longer describes itself by naming the control it defeats.**
  Three surfaces described the curl_cffi strategy as "browser-like headers for
  WAF/Cloudflare bypass" (agent hints), "browser-like headers for WAF/Cloudflare
  bypass" (the OpenAPI `strategies` field), and "evades naive fingerprinting"
  (the README strategy table). Nothing in those strings says the service
  *reports* a challenge instead of clearing one, which is the behavior a reader
  needs to know. They now describe the mechanism and the condition it serves:
  a browser-shaped TLS fingerprint, for sites that reject non-browser clients.
- **BREAKING:** `/v1/capabilities` no longer advertises a `cloudflare_bypass` field, and
  `credential_bypass` is now `credential_injection`. The field's own comment
  said "do not advertise bypass" while the line below it advertised bypass. It
  is removed rather than reworded because a Cloudflare interstitial *is* a
  challenge, so the existing `captcha_solving: false` already states it;
  keeping it meant carrying a second, redundant declaration with the offending
  noun in it. `credential_injection` matches the verb the guidance prose, the
  MCP tool descriptions and `AGENTS.md` already use. **Callers reading
  `features.credential_bypass` must read `features.credential_injection`;
  `features.cloudflare_bypass` is gone, and `features.captcha_solving` is the
  statement that challenges are not solved.**
- **BREAKING: every configuration knob is now `CRAWLUTRON_*`.** The service is
  `adambarber/crawlutron`, and the `BROWSER_CRAWLER_` prefix no longer names
  anything. This covers the `BROWSER_CRAWLER_BASE_IMAGE` build arg in the
  Dockerfile, all ~90 environment variables in `.env.example` and
  `docker-compose.yml`, and the settings prefixes read by `app/settings.py`.
  Deployments that set these variables by name must rename them, or they will
  silently fall back to defaults -- `CRAWLUTRON_HOST` still defaults to
  loopback, so the service starts and binds `127.0.0.1` rather than failing
  loudly.
- `SERVICE_ID` is `crawlutron` and `REPOSITORY_URL` is
  `https://github.com/adambarber/crawlutron`, so the user-agent now announces
  the real remote. `pyproject.toml` metadata, `CONTRIBUTING.md`'s clone URL and
  the uvicorn log config's `service_name` are pinned to the same spelling by
  `tests/test_project_urls.py`.

### Performance
- **Concurrent robots screens** (app/crawl/recursive.py,
  app/crawl/engine.py): the recursive batch screen, discovered-link screen,
  and the non-recursive batch screen now `asyncio.gather` their
  `robots_allows`/`robots_crawl_delay` checks instead of awaiting them one
  URL at a time. Same closures, same `robots_cache` chokepoint and SSRF
  semantics; in-batch duplicates keep visit-once behavior.
- **LLM metadata off the event loop**
  (app/api/response_shaping.py): new additive `aformat_crawl_item` /
  `abuild_unified_result` / `abuild_unified_crawl_response` offload the
  per-item lxml metadata parse with `asyncio.to_thread` (items gathered
  concurrently) and are now used by the async runner, render, webhook, MCP,
  and job-runner paths. Sync shapers are unchanged. `crawl_endpoint.py` is
  owned by another stream and keeps the sync call until it adopts the seam.
- **Sync trafilatura off the loop**
  (app/crawl/strategies/http.py, app/crawl/engine.py): both `_extract_html`
  call sites (HTTP strategy result, browser strategy result) now run via
  `asyncio.to_thread`, matching the neighboring document/full-pipeline
  extractions. `strategies/browser.py` needed no change: its `_extract_html`
  is already async browser I/O (`page.content()`), not a CPU parse.
- **Shared curl_cffi session**
  (app/crawl/strategies/http.py, app/main.py, tests/conftest.py):
  `_default_cffi_fetcher` reuses one `AsyncSession` per event loop
  (`get_cffi_session`, mirroring `get_async_http_client` with loop-change
  rebuild) instead of building one per request; the per-request timeout now
  travels on `get()` and the stream handle is released after STREAM_END so
  the shared session can serve the next request. Shutdown closes it via
  `close_cffi_session` (lifespan + test reset). The streaming-cap fake now
  records construction (shared protocol has no per-request enter).
- **RAM-scaled body cap uses live concurrency**
  (app/crawl/engine.py, app/crawl/strategies/http.py):
  `_execute_crawl_chain`/`_http_strategy_result` accept the crawl's
  `global_concurrency` (threaded from the engine's active settings) so
  `_effective_max_content_length` honors the operator-configured concurrency
  instead of always re-reading defaults and falling back to the settings
  floor. Omitted (tests, direct callers) preserves the old floor behavior.
  True live-dispatch scaling (admission-derived concurrency) remains future
  work -- this threads the configured value, not a measured one.
- **Browser fixed-overhead knobs are configurable, defaults frozen** (app/constants.py, app/settings.py,
  app/crawl/strategies/browser.py, .env.example): lazy-scroll steps/delay
  and the cookie-harvest wait are now settable via env/CrawlerSettings and
  per-render `browser_profile` keys (`lazy_scroll_steps`,
  `lazy_scroll_delay_seconds`, `cookie_harvest_timeout_seconds`). Shipped
  defaults are byte-identical (3 / 1s / 5.0s); ready-state and challenge
  behavior are untouched.
- **Persistent markitdown pool** (app/crawl/extraction.py):
  `_extract_document` is served by persistent pool workers (spawned on
  demand up to `CRAWLUTRON_EXTRACTION_DOC_WORKERS`) instead of one
  process per document; markitdown imports once per worker lifetime.
  Timeout-kill semantics kept: a hung/dead worker is terminated and reaped
  per task, its slot respawned on next acquisition. The hermetic
  `TEST_MARKITDOWN_*` seam is snapshotted per task parent-side (persistent
  workers would otherwise see spawn-time env); all extraction-bounds tests
  pass unchanged.
- **Sync post-fetch parsing off the loop**
  (app/crawl/extraction.py, app/crawl/engine.py,
  app/crawl/strategies/http.py): new `extract_post_fetch_discovery` parses
  once and derives frames/forms, discovered links, JS endpoints, and the
  pagination hint; both strategy result builders run it via
  `asyncio.to_thread` and keep their exact gating/egress-filter contracts.
  Only plain data returns (the lxml tree never escapes).
- **Per-fetch env scan hoisted off the httpx path (tier-2 T1)**
  (app/crawl/strategies/http.py): `get_async_http_client` now defers the
  `CrawlerSettings.from_mapping()` env walk to the actual construction
  branches. The hot path (pooled client alive on the current loop) consults
  env zero times per fetch; every rebuild still reads operator pool limits
  fresh, and loop-change rebuild/pending-close semantics are unchanged.
- **Normalization guard memoized (tier-2 T2)** (app/validation.py):
  `normalize_public_http_url` now consults the
  `CRAWLUTRON_ALLOW_PRIVATE_TARGETS` decision through a raw-value-keyed
  memo (any env change, including tests, is visible on the very next call;
  unrecognized values keep the guard ON) and classifies hosts through a
  bounded pure-function `lru_cache` verdict — per-anchor normalization no
  longer pays an env parse plus an `ipaddress` exception per call. Resolver-
  numeric forms (`2130706433`, `0x7f000001`) keep their private verdict.
- **Single batch-gate pass per request (tier-2 T3)**
  (app/crawl/payload_helpers.py, app/crawl/engine.py,
  app/api/crawl_endpoint.py, app/api/crawl_executor.py,
  app/api/async_crawl_runner.py): `checked_payload_urls` gained an optional
  forwarded-batch parameter and the endpoint hands its gated result through
  `execute_crawl`/`run_async_crawl` into `crawl_request`, so a request's URL
  batch is parsed once instead of at both the boundary gate and the engine.
  A forwarded batch skips re-derivation but not the cap: the boundary gate
  and the engine-internal ceiling both still reject oversized batches.
- **HttpCache eviction snapshot bounded to the overflow (tier-2 T4)**
  (app/crawl/cache.py): `_evict_if_needed` draws the doomed keys through
  `itertools.islice` instead of materializing the full key list (up to 100k)
  per over-cap store. Same FIFO insertion-order victims, still snapshot-
  before-pop, still entirely under `self._lock`.
- **Spill-GC cadence gate (tier-2 T5)** (app/crawl/engine.py):
  `_ResultSpill` no longer runs its stale-spill sweep (`os.listdir` of the
  temp dir plus per-file `getmtime`) on every crawl request. A module-global
  monotonic stamp bounds it to one sweep per 300 s per process, and the first
  construction always sweeps, so leftovers from killed runs are still
  reclaimed early. The `gc_age_seconds` floor is untouched: no active spill is
  ever unlinked.
- **Sitemap freshness seeding batched off the event loop (tier-2 T7)**
  (app/crawl/engine.py): each freshness verdict reads and parses the
  candidate's envelope from disk, so filtering a big sitemap inline stalled
  every concurrent crawl. ``seeds_for`` now collects the normalized,
  scope-filtered candidates and runs the whole batch through one
  ``asyncio.to_thread(_stale_seeds, ...)`` call. Key composition, the full
  strategy chain, and the fail-open contract (unreadable/expired/missing →
  not fresh → URL still crawled) are untouched.
- **Cooldown purge amortized off the per-429 path (tier-2 T8)**
  (app/scheduler.py): every rate-limited result reaches
  ``set_domain_cooldown``, which rebuilt both the cooldown and circuit dicts
  per call — quadratic in the number of 429s. The purge now fires at most once
  per 100 sets (``run`` keeps its up-front purge), the two clocks stay
  separate, only expired entries drop, and a domain's own fresh cooldown is
  never removed.
- **Admission sizing: honest per-task RAM + no browser double-scale
  (tier-2 T9/T10)** (app/resource_pressure.py): the per-HTTP-task estimate
  rises from 12 MiB to 48 MiB — a max-length body plus the
  trafilatura/lxml parse state is 40+ MiB, so the old figure over-granted by
  ~4x and the box swapped at the review's live scale. The estimate stays a
  single env-overridable constant that both the admission ceiling and the
  httpx pool sizing divide by, so the two cannot drift. Separately,
  ``browser_capacity`` no longer shrinks against the pool's own reserved
  footprint: capacity is full while usage stays within the fraction
  ``derive_browser_pool_size`` assumed, ramps to zero as the box exhausts,
  and the CPU gate still applies — browsers are never admitted when
  saturated.
- **ContentCache unlinks moved off the lock (tier-2 T6)**
  (app/crawl/content_cache.py): `_drop_locked` (and the TTL-expiry path in
  `_read_envelope`) now mutate the index, byte accounting, and RAM mirror
  under the lock while collecting the doomed `(key, path)` pairs; the unlinks
  run in `_unlink_doomed` after release, so disk I/O never blocks the
  event-loop thread that contends on the lock. `_unlink_doomed` re-validates
  each pair under the lock first: `_path_for` is deterministic, so a key re-set
  between eviction and unlink reuses the same path and a blind unlink would
  destroy the live entry — a still-registered path is spared. LRU order,
  byte accounting, and the "never evict the just-written entry" contract are
  unchanged; `prune`/`clear` (cold maintenance paths) keep their inline
  unlinks.

### Fixed
- **The agent contract doc was unreachable from every doc that cites it.**
  `README.md` and `AGENTS.md` both send agents to
  `docs/references/crawlutron-agent-contract.md`; the file on disk was
  `browser-crawler-agent-contract.md`. The rename swept link targets and
  missed the filename, so the reference chain a new integration is expected
  to follow dead-ended at the first hop. Renamed via `git mv` so history
  follows the file, and the document's own title and opening line now carry
  the brand. `docs/review-and-refactoring-plan.md` keeps the old name on
  purpose: it is stamped ARCHIVED and describes a topology that no longer
  exists, so its references are history, not navigation.
- **The OpenAPI document still called the service something else.** The
  rename matched hyphen and underscore spellings; the `/docs` title is
  spaced and title-cased, so it shipped as "Browser Crawler Execution
  Service" while the discovery payload beside it announced `crawlutron`. Two
  surfaces a client reads, disagreeing, in the same response tree. Now pinned
  positively in `tests/test_project_urls.py` rather than added to a ban list,
  because a pin fails on the next spelling and a ban list only fails on the
  one someone remembered to write down.
- **README documented two test suites that no longer exist.**
  `tests/test_adversarial_fixes.py` and
  `tests/test_adversarial_round3_fixes.py` were consolidated into the themed
  modules under `tests/invariants/`, but the adversarial-coverage section
  still told readers to open them, listing ten attack classes against
  filenames that had been gone for some time. Repointed at the suites that
  actually carry each class now, every one verified to contain the case
  before it was cited. A coverage claim is worth exactly as much as its
  accuracy: pointing a security reader at a missing file teaches them the
  coverage is not where they were told, which is worse than saying nothing.
- **robots.txt agent matching is case-insensitive for the query side too**
  (`app/crawl/robots_parse.py`): `robots_groups` lower-cased the file's
  `User-agent` lines but `select_robots_values` compared that lower-cased
  string against the token as passed, so case-insensitivity silently depended
  on every caller supplying an already-lower-case token. A mixed-case token
  matched no group and fell through to the `*` group -- answering a question
  RFC 9309 2.2.1 makes case-insensitive by discarding the site's own rules.
  The token is now normalised at the comparison, so the guarantee lives with
  the code that depends on it rather than in each caller's discipline.
- **Doc-pool child-death assertion now checks the real invariant (M8)**
  (app/crawl/extraction.py, tests/crawl/test_extraction_bounds.py):
  `test_extract_document_child_death_returns_empty` asserted
  `multiprocessing.active_children() == []` — a process-global invariant the
  persistent pool legitimately violates, since it is sized at 2 and keeps
  idle workers alive across calls. Under random ordering it failed ~40% of
  runs whenever a prior test left a healthy idle worker pooled. Replaced with
  the per-worker contract via a tests-only `_doc_pool_health()` snapshot: no
  worker left marked busy mid-task, and every dead worker is reaped (exitcode
  set) rather than a zombie. No production behavior changed.
- **Env-decision memo is resettable by tests (tier-2 T2 follow-up)**
  (app/validation.py, tests/test_validation.py): the memoization added for T2
  is process-global, so a value cached by one test made another's first call
  hit the cache and look un-scanned. Added `_reset_guard_memo()` and the
  spill-GC counterpart for the T5 stamp; the affected tests reset before
  measuring, keeping the memo's production contract (raw-value-keyed, so any
  env change is visible on the next call) intact.
- **`discovery_flags`' docstring no longer contradicts its own code**
  (app/crawl/policy.py): it said "each channel inside it defaults to on" and
  then returned `DiscoveryFlags(False, False, False)`. The code was right —
  `DiscoveryConfig` declares every channel `= False` and its docstring says so
  — but the docstring was the surviving half of the inverted-defaults fix, and
  it was the artifact a reader consults to learn what absence means. Rewritten
  to say what the function decides: an absent block and an explicit
  `{"discovery": {}}` are the same policy, because `true` is the only thing that
  turns a channel on and both mean fetch nothing. The old text also credited
  the wrong cause, reading the block's optionality as the hazard when
  optionality is exactly why the equivalence has to be decided in one place.

### Added
- **A gate on the doc gate's own scope**
  (`tests/invariants/test_contracts.py::test_live_documents_are_derived_not_enumerated`).
  The first cut of the live-document reference gate enumerated the nine
  documents it checked, which made the gate's own coverage a hand-maintained
  list that drifts silently: a document added later inherited no coverage and
  nothing failed. That is not hypothetical -- `docs/typing-plan.md` sat
  outside the enumerated nine for exactly as long as it had seven dead paths,
  and no gate noticed. Coverage is now derived from the tree, so every
  markdown file is checked unless it is *named* as an exception, and the
  maintained list is inverted: four exemptions, each a deliberate decision
  about a record that is supposed to cite deleted files, and each carrying the
  reason. The `CHANGELOG`, the dated adversarial review and the ARCHIVED
  refactoring plan are exempt because rewriting their citations would falsify
  them. The typing plan joins them on a different basis: it is a snapshot
  audit whose module references predate a reorganisation of `app/crawl/` and
  `app/api/`, so it is now stamped as a snapshot rather than left to read as a
  map of the tree. Repointing fifty audit entries by eye was rejected as a
  claim of verification nobody performed. A performance review that had been
  uncovered the whole time is now covered and passes.
- **Deployment-manifest drift gate**
  (`tests/invariants/test_contracts.py::test_deployment_manifest_sets_no_dead_knob`):
  a key `docker-compose.yml` sets that nothing in `app/` reads is the
  quietest failure in the configuration surface. The service does not crash
  and nothing 5xxs -- the deployment starts, the operator believes the
  setting is applied, and the default silently wins. From the service's
  point of view the key was never mentioned, so no runtime signal can
  surface it. The manifest is now checked against the same consumer set the
  `.env.example` direction uses, because compose is a declaration of intent
  from the same authority, only later and more consequential: `.env.example`
  describes a local checkout, while a key set here is what a real deployment
  runs with. Verified by mutating a live key to `CRAWLUTRON_LOG_LEVL`; the
  build goes red and green again on revert. Clean as of this change, which is
  the point of adding it while it is still clean.
- **Live-document reference gate** (`tests/invariants/test_contracts.py`):
  `test_live_docs_point_at_paths_that_exist` walks the backticked repo paths
  in the nine documents a reader arrives intending to follow, and fails when
  one does not exist. It is deliberately scoped, because the alternative
  reading is wrong: a CHANGELOG entry naming a since-deleted module, an
  ARCHIVED plan describing a topology that no longer exists, and a dated
  review quoting the tree it reviewed are all accurate, and "fixing" them
  would falsify the record. Only navigational claims are checked. Spans must
  contain a separator, since a bare `robots.txt` in prose is a basename being
  discussed rather than a path being pointed at. It found the contract-doc
  break above. Note the convention it had to match: this tree cites paths in
  backticks, not markdown links, so a conventional link checker finds four
  links in fourteen documents and misses all of it.
- **Reverse configuration-drift gate** (`tests/invariants/test_contracts.py`):
  `test_documented_browser_knobs_have_consumers` walked documented ->
  consumed, so a knob published in `.env.example` that nothing reads fails
  the build. Nothing walked the other way, so a knob the service *did* read
  could ship undocumented and still be green. That failure has no crash to
  notice: the setting works, and the only way to find it is to read the
  source. `test_every_consumed_knob_is_documented` closes it using the same
  `_environment_reader_reference` detector as the forward direction, so the
  two agree by construction rather than by two hand-maintained lists
  happening to line up. It found three on first run --
  `CRAWLUTRON_SITEMAP_LASTMOD_FUTURE_SKEW_SECONDS`,
  `CRAWLUTRON_EXTRACTION_DOC_ACQUIRE_TIMEOUT` and
  `CRAWLUTRON_DOC_TASK_RAM_BYTES` -- now documented in `.env.example`. The
  last is the one worth noting: it is a FIXED reservation for the persistent
  markitdown workers, not a per-task estimate, and it is the knob that
  decides whether the service is admitted work it cannot fit.
- **Web search endpoint** (`POST /v1/search`): discover public URLs from a
  query via an operator-configured provider. Provider abstraction ships
  Tavily, Brave, and Serper (keyed) plus a keyless DuckDuckGo fallback;
  `CRAWLUTRON_SEARCH_PROVIDER=auto` activates the first provider whose
  `*_API_KEY` is set. Result URLs pass the same public-URL/SSRF validation as
  crawl targets; provider failures surface as `502` and a selected-but-
  unconfigured provider as `503` naming the missing variable. Search metrics
  are exposed on `/metrics` (`crawler_search_*`).
- **MCP 2026-07-28 endpoint** (`POST /mcp`): stateless streamable-HTTP server
  exposing `search`, `crawl`, `map`, and `render` as MCP tools. Implements the
  stateless protocol (no `initialize` handshake): `server/discover`,
  `tools/list`, `tools/call`, per-request `_meta` validation, and
  header/body validation for `MCP-Protocol-Version` / `Mcp-Method` /
  `Mcp-Name`, with Origin (DNS-rebinding) rejection. Tools delegate to the
  same handlers as the REST endpoints, so all four strategies and every
  extraction method are available over MCP.
- **Discovery payloads** now advertise search + MCP: `/v1/capabilities` gains
  typed `search` and `mcp` capability blocks plus `web_search`/`mcp` feature
  flags; `/v1/agent-hints` and `/.well-known/crawler-service` link the new
  endpoints. Discovery routers now read settings via `Depends(get_settings)`
  instead of closing over import-time values.
- **Removed compatibility code**: `app/main.py` backward-compat exports
  (`_format_crawl_item`, `_render_result_to_unified`, `build_unified_crawl_response`)
  and the accepted-but-ignored `HttpCache(db_path=...)` parameter are gone;
  `/v1/render` now uses the injected settings like every other endpoint.

### Security
- **`CRAWLUTRON_ALLOW_PRIVATE_TARGETS` now warns when it disables the SSRF
  guard** (app/validation.py). The hatch turns off the only thing between a
  caller-supplied URL and `169.254.169.254`, and it did so silently -- a
  misconfigured deployment looked identical to a correct one. The single
  decision chokepoint now logs a `SSRF private-target guard DISABLED` WARNING
  naming the variable, the raw value, and what is now reachable. It is warned
  at the decision rather than at each call site because that path runs per
  fetch; the existing raw-value-keyed memo makes it exactly once per distinct
  value, so a production process logs it once. `README.md`, `SECURITY.md` and
  `.env.example` now say so. This closes the second half of the recommendation
  in `ADVERSARIAL_REVIEW_BROWSER_CRAWLER.md`; the first half (a literal true
  value required, not merely a non-empty one) already held via `parse_bool_env`.
- The same hatch was read in three places. `app/crawl/strategies/pinned_transport.py`
  re-read the environment directly at both SSRF entry points through a
  function-level import, bypassing the memoized reader, so the parse cost and
  the decision were duplicated. All three call sites now go through
  `_allow_private_targets()`, and the two function-level imports are gone.
- **Thread-safety on `_DNS_CACHE`** (app/crawl/strategies/http.py): the
  module-level DNS resolution cache is now guarded by a `threading.Lock`,
  fixing a real concurrency bug where `_resolve_ips` (called from both
  the event loop and worker threads via `asyncio.to_thread`) could
  corrupt state or drop entries.
- **Thread-safety on `robots_cache`** (app/crawl/engine.py): the
  module-level robots.txt cache is now guarded by `_ROBOTS_CACHE_LOCK`.
  Concurrent reads, the `_purge_robots_cache` sweep, and the
  `_cap_robots_cache` eviction are all atomic.
- **Per-request cookie namespace** (app/crawl/strategies/http.py,
  app/crawl/engine.py): multi-tenant callers can now set
  `request_profile.cookie_namespace` on each crawl. Cookies are keyed
  by `(namespace, host)` so concurrent calls from different tenants
  cannot leak cookies through the shared `_GLOBAL_COOKIE_JAR`.
- **Body-size guard** (app/main.py): request bodies are capped at 16 MiB
  via an ASGI middleware that wraps `receive()`. A malicious caller
  can no longer exhaust memory with an oversized JSON before pydantic
  validation runs.
- **CSV injection guard** (app/output_formats.py): cells in attacker-
  influenced columns (`error`, `classification`, `status`) whose first
  character is `=`, `+`, `-`, `@`, TAB, or CR are prefixed with a
  single quote so spreadsheet apps do not execute them as formulas.
- **storage_state path allowlist tightened** (app/crawl/strategies/
  browser.py): only the operator-controlled `settings.cache_dir` is
  accepted. The previous `/tmp` fallback (world-readable on most hosts)
  is removed.
- **Size guards on HTML processing** (app/classify.py,
  app/crawl/firecrawl_format.py): HTML passed to the noise-stripping
  regex and the lxml-based metadata extractor is truncated to 4 MiB
  before processing, capping the worst-case RAM exposure of the
  classification and metadata paths.

### Hardening
- **The jscpd gate can now see a clone that spans two trees**
  (tests/_jscpd_gate.py, tests/test_jscpd_gate.py): the gate ran one scan
  invocation per root, which is what lets a report say which tree it is
  talking about — and which means a fragment shared between `app/` and
  `tests/` was compared by no invocation at all. `tests/_recursive_harnesses.py`
  re-spelling production's `run_recursive_crawl` signature was exactly that: a
  20-line mirror every per-tree pass was blind to by construction. A second
  spanning scan now runs over every root; jscpd reports a name with its root
  stripped in that mode, so the gate re-attaches the root against the
  filesystem. A name in no root, or in two, is no longer guessed into a
  silent pass — unresolvable raises, ambiguous is kept and reported, because
  dropping a real cross-tree clone reads as clean.
  `test_per_root_scans_alone_cannot_see_a_cross_tree_clone` asserts the gap
  rather than trusting this note, and fails if the per-root scans ever grow the
  ability to compare across roots, at which point the spanning invocation
  should be deleted rather than kept paying for a second full scan every run.
- **Duplicate floor 4/39 → 4/25, and the two survivors are declared**
  (.pre-commit-config.yaml, jscpd-expectations.json): 4/39 was a number that
  made this tree look clean. Measured at 4/25 it was not — `app/` carried 6
  pairs in the 26-36 token band and the tree carried one more that no per-tree
  gate could see. All seven are gone, and the floor is now 4/25, so the band
  most real duplication in this tree measures at is enforced rather than
  assumed empty. The two remaining `app/` pairs are **declared** rather than
  hidden above the floor: each is duplication a machine-checked guard exists to
  compare (the `HttpFetchContract` twin signature, the selector pair), so the
  tree holds the copy on purpose and the guard is what makes holding it safe.
  Each entry pins its fragment by content hash and names the guard, so editing
  either side turns the entry into an unsanctioned clone and sends the change
  back through review.
- **The recursive-crawl harness derives its contract instead of restating it**
  (tests/_recursive_harnesses.py, tests/crawl/test_recursive_dispatch.py):
  `drive_recursive_crawl` named all twenty of `run_recursive_crawl`'s keywords
  a second time. It now takes the uncontested defaults from `_crawl_defaults`
  and binds every keyword against production's own `inspect.signature`, so the
  list of keywords exists in exactly one place. This is not a `**kwargs`
  catch-all, and the tests say so by measurement: a harness that silently
  dropped unknown keywords fails both contract tests and passes the other nine.
  A misspelled `max_dept` names itself instead of becoming a bound nobody set,
  and a keyword production has dropped cannot be handed over as a default the
  caller chose nothing about.
- **Bounded `_PENDING_CLIENT_CLOSE`** (app/crawl/strategies/http.py):
  the async-pending client close list is now bounded at 32 entries with
  a `threading.Lock`, preventing unbounded growth across a stream of
  rapid event-loop changes.
- **`_ResultSpill` FD-leak fixed** (app/crawl/engine.py): the constructor
  now closes the raw `mkstemp` fd if `fdopen` fails; `close()` deletes
  the auto-created spill file; `_gc_stale_spills` failures are non-fatal.
- **`lifespan(None)` smell removed** (app/main.py): the lifespan handler
  is now an `@asynccontextmanager` directly, removing the misleading
  `lifespan=_lifespan(None)` indirection.
- **The MCP constant registry can no longer claim coverage it lacks**
  (app/mcp/constants.py, tests/test_safety_invariants.py):
  `MISSING_REQUIRED_CLIENT_CAPABILITY` and `META_CLIENT_INFO` were declared and
  read by nothing — the 2026-07-28 spec requires only `protocolVersion` and
  `clientCapabilities` in `_meta`, so no path ever raised the code or read the
  key. Deleted. A registry entry with no consumer is an untested list of
  strings, and an untested error code is worse than an absent one because it
  reads as coverage. `MCP_PROTOCOL_VERSION` is kept but unexported: it is read,
  by the `SUPPORTED_PROTOCOL_VERSIONS` list derived from it in the same module,
  and exporting a name only its own module reads is the claim that was wrong.
  `test_every_protocol_constant_is_produced` now holds the line — every name in
  the module's `__all__` must be read somewhere under `app/`, swept tree-wide,
  and a registry the sweep cannot read is itself a finding.
- **A type suppression that suppresses nothing is a build failure**
  (pyrightconfig.json, tests/test_pyright_budget.py): pyright now reports
  unnecessary ignore comments, and
  `test_pyright_detects_its_own_suppressions_decaying` asserts the rule is on,
  because the failure is invisible from the tree — a stale suppression and a
  working one are the same bytes to every other gate. Eight suppressions were
  suppressing nothing, each with a comment explaining why it was needed; the
  five in `app/api_models.py` had never suppressed anything, from the commit
  that added them onward. One of the eight was not a suppression at all but a
  prose sentence inside a comment block quoting the literal directive, which
  pyright honors on that line — so the paragraph had been muting itself.
- **Pyright no longer speaks for mypy about ignores**
  (pyrightconfig.json, app/cgroup_sense.py, app/crawl/strategies/http_client_pool.py):
  `enableTypeIgnoreComments: false`. While pyright honored mypy's `# type:
  ignore` comments, a suppression that satisfied the fail gate could hide a
  finding from the checker whose whole job is annotation lies — and did, five
  times. `_CGROUP_CACHE` was declared `dict[str, tuple[float, object]]` while
  its only writer stores a stripped string or `None`, so returning it needed a
  bare `# type: ignore[return-value]` and the function's own contract went
  unchecked; the value type is now declared, which deletes the suppression and
  lets both checkers read the code. Mypy keeps its own comments
  (`warn_unused_ignores` is on in mypy.ini).
- **Pooled client loop binding has one chokepoint**
  (app/crawl/strategies/http_client_pool.py): `_bound_loop` was written at four
  sites — two builders, each with its own no-running-loop branch — and every one
  carried a bare `# type: ignore[attr-defined]`, because neither `httpx` nor
  curl_cffi declares the attribute; this module installs it. A `_LoopBound`
  Protocol, a `_bind_to_running_loop` writer and a `_loop_bound_to` reader
  replace four suppressions, two inline branches and a defaulted `getattr`. `None`
  is documented as an answer rather than a failure: a client built outside a loop
  matches nothing, so the next `get` rebuilds instead of handing out a client
  this loop cannot use. No behavior change.
- **Two request models stopped restating a field their base class declares**
  (app/api_models.py): `CrawlRequest` and `RenderRequest` each subclass
  `_SingleUrlSourceRequest` — whose docstring says the rule is "attached once"
  and joining the family is "a base class rather than a copy" — and then each
  restated `url` with a comment admitting it was inherited, and restated the
  validator too. **Wire-visible, documentation only:** the `url` field
  description in the generated OpenAPI schema for both models is now the base's
  wording (`Single public HTTP(S) URL. Use either url or urls.`) instead of the
  per-model variant ending "to crawl" / "to render", which is the drift the base
  class's docstring had already ruled on. Field sets are unchanged and
  exactly-one-of is still enforced: both supplied is rejected, either alone is
  accepted, neither is rejected. Found by jscpd once the trailing suppression
  comments stopped distinguishing the two copies.
- **Both failure primitives classify and log through one chokepoint**
  (app/crawl/result_helpers.py, tests/crawl/test_error_disclosure.py):
  `_engine_result_from_exception` and `_crawl_result_from_exception` are the same
  operation at two layers -- an `EngineCrawlResult` and the render pipeline's
  `CrawlResult` -- and each spelled out its own `_logger.warning(..., extra={...})`
  with the same three keys, one wrapped over six lines and the other on one. A
  `_classify_and_log` primitive now owns classify-then-log-then-decide-retryable
  for both, with the event name as a parameter because it is the only thing that
  should differ between them. The drift was invisible from either side: an
  operator grepping the failure stream for `error_detail` would have found the
  engine path, not found it on the render path, and concluded the render path had
  stopped logging the one field that makes a failure debuggable.
  `test_both_failure_primitives_log_the_same_server_side_payload` drives both
  real primitives and compares the two `LogRecord`s' attribute sets to each other
  rather than to a hand-written key list — a key-list snapshot passes whenever
  both paths are wrong in the same direction and goes stale the first time
  someone legitimately adds a field. It also asserts the raw detail is present in
  the log and absent from each result's wire form, which is the asymmetry those
  keys exist for. Proven to bite by restoring the pre-extraction shape with one
  key drifted off the engine path's payload; both records now report
  `funcName == "_classify_and_log"`.
- **The pydantic default detector reads both spellings of a field's default**
  (tests/test_pyright_budget.py, app/api_models.py):
  `test_pydantic_defaults_stay_where_a_checker_can_see_them` read only the
  assignment's value, so it saw `x: T = Field(None, ...)` and was blind to
  `x: Annotated[T, Field(None, ...)] = None` — the dominant spelling in
  `app/api_models.py`, and the one both dead `url` re-declarations used. Four
  fields stated one default twice, in `Field()` and on the assignment; the
  default now lives on the assignment, once. Declared and OpenAPI schema
  defaults verified unchanged per model (`''`, `''`, `False`).
- **The cycle guard is applied in one function, not once per container arm**
  (app/models.py, tests/test_to_dict.py, tests/test_safety_invariants.py):
  `_to_jsonable_impl` carried the descend-and-give-up sequence inline in all
  three container arms, so the guard existed in four places while `_entered`'s
  own docstring claimed "One function rather than three copies of the four
  lines" — the dedup was intended and never finished, and the claim made it
  worse, because a reader checking that sentence would have believed it.
  `_build_guarded` owns the check and every arm routes through it, which is
  what makes the guard structural: a new container kind arrives as a new
  `isinstance` arm, and nothing about adding one makes the check mandatory
  there. The dict and sequence arms also had no test of their own (the
  existing cyclic-graph test breaks its cycle on re-entering a *dataclass*),
  so both were carried by a guard nothing exercised.
  `test_the_cycle_guard_is_applied_in_exactly_one_place` pins the single owner
  across `app/`, proven red on the three inline sites.
- **The `_meta` key rejection reason is authored once, and its wire strings
  are pinned** (app/mcp/protocol.py, tests/mcp/test_mcp.py,
  tests/test_safety_invariants.py): `validate_meta` checked two required
  `_meta` keys and, for each, spelled out the same absent-versus-wrong-type
  reason. Telling a client that forgot the key from one that sent the wrong
  shape is the caller's only way to act on the correction (N-6), so the
  sentence is a contract, and two copies can drift both ways at once: the arms
  can stop agreeing on wording, or one can stop distinguishing the cases, with
  the suite green either way. `_require_meta_key` owns the check for both
  keys, and `label` is the type as the spec names it rather than the Python
  class, because the string goes on the wire. No test asserted any of the four
  messages before;
  `test_meta_key_rejection_separates_absent_from_wrong_type` now pins all of
  them over the real endpoint, and
  `test_the_meta_key_rejection_reason_is_authored_once` matches the literal
  rather than the shape so a re-spelled arm is reported as the drift it is.
  `_enclosing_calls` is generalized into `_enclosing_nodes` so a detector can
  read ownership of any node kind — a message string is a contract too, and
  neither is reachable through `nodes_of`.
- **A duplicated comment block is now visible to the suite**
  (app/crawl/content_cache.py, tests/test_safety_invariants.py): the same
  six-line note about the content cache's eviction window sat at both write
  call sites, and the jscpd gate could not see it — jscpd matches tokens, so a
  duplicated comment contributes nothing and the enforced 4/25 floor reported
  it as zero clones. It was also wrong. It claimed the call "briefly releases
  `self._lock` between the write and the eviction"; `_lock` is a plain
  `threading.Lock` and both call sites invoke `_enforce_limits()` outside their
  `with` block, so nothing is released — the window is the caller's release to
  `_enforce_limits`' acquisition, and a caller holding the lock there would
  deadlock. Two copies kept in step by hand had already drifted into
  describing a mechanism the code does not have. The note now lives on
  `_enforce_limits`, where its acquisition closes the window, stating the
  corrected mechanism. `test_no_comment_block_is_written_twice` gates the whole
  class — no contiguous three-line comment appears twice in `app/`, normalized
  on whitespace because a re-indented copy is how these forks arrive, and
  matched on consecutive lines so a blank line or a statement ends a block.

### Round 3 hardening (from REVIEW_ROUND3.md)
- **Body-size middleware: keep-alive poisoning fixed** (app/main.py):
  truncated requests now get ``Connection: close`` on the response and
  the receive() wrapper continues draining `original_receive()` until
  `more_body=False`, so a single oversized request cannot poison the
  underlying keep-alive socket for subsequent legitimate requests
  (REVIEW_ROUND3.md C1/C4).
- **Body-size middleware: per-chunk trailers preserved** (app/main.py):
  the capped receive() now merges `{**message, ...}` rather than
  `dict(message)` so ASGI per-chunk `headers` survive truncation
  (REVIEW_ROUND3.md C3).
- **`_PENDING_CLIENT_CLOSE` cap releases sockets synchronously**
  (app/crawl/strategies/http.py): the dropped client now has its
  synchronous `httpx.AsyncClient.close()` invoked before being
  released, so the httpcore connection pool is reclaimed even when
  no event loop is running. Dedupe check and cap check are now
  atomic under the same lock acquisition
  (REVIEW_ROUND3.md B1/B2/B3).
- **`_BROWSER_RAM_FRACTION` import fixed** (app/crawl/strategies/
  browser_pool.py): the symbol was referenced but not imported,
  which only surfaced when the lifespan ran (TestClient,
  uvicorn startup). The import is now correct.
- **`coverage_helpers.py` import fixed** (tests/coverage_helpers.py):
  `BROWSER_HEADLESS`, `EXTRACTION_DOCUMENT`, and `EXTRACTION_TRAFILATURA`
  were imported from `app.crawl.engine` but actually live in
  `app.constants`. The wrong import caused 27 gap-coverage test
  modules to fail collection. The constants now come from
  `app.constants` directly.

### Open-source extraction (adversarial review)
- **`local_service_logging` shim promoted to module** (app/logging_config.py):
  the parent-monorepo dependency on `local_service_logging` (imported
  via `try/except ImportError`) is replaced with a standalone
  `app.logging_config.configure_service_logging` helper. The new
  helper emits JSON-line records with rotating file output and is
  importable from any clone without the parent monorepo tree.
- **`shared/python` path references removed** (tests/conftest.py,
  scripts/probe_session.py): the `SHARED = ROOT.parent / "shared" /
  "python"` injection that tried to add the parent monorepo's shared
  helpers to `sys.path` is gone. The suite and the probe now rely on
  the package's own `app.*` modules.
- **`_ResultSpill.close()` double-unlink fixed** (app/crawl/engine.py):
  the auto-cleanup branch was unlinking the owned temp file twice;
  the duplicate `os.unlink` is removed (one happy-path unlink is
  enough).
- **SSRF env var parsing centralized** (app/validation.py,
  app/crawl/strategies/http.py, app/main.py): every
  `CRAWLUTRON_ALLOW_PRIVATE_TARGETS` check now goes through
  `app.validation.parse_bool_env`, which treats `1`/`true`/`yes`/`on`
  as truthy (case-insensitive). The previous `== "1"` literal in
  three files agreed on the value but rejected `"true"`/`"yes"`/`"on"`.
- **Browser-pool `None` guard with URL validation** (app/main.py):
  `_execute_crawl` now raises a clean 503 when the pool is None AND
  the request needs a browser step, after first validating the URL
  so an invalid URL still surfaces as a 422. Requests that only use
  HTTP strategies are unaffected by the pool state.
- **Hardcoded `/app/*` paths portable** (app/settings.py): the
  `log_file_path`, `cache_dir`, and `cookie_jar_path` defaults are
  now relative (`./logs/...`, `./cache/...`) so a fresh clone works
  without an `/app/` layout. Parent directories are created on
  startup.
- **Identity strings normalized** (app/crawl/strategies/http.py,
  app/crawl/engine.py, app/main.py, app/constants.py): the
  `dev-stack-crawlutron/0.2 (+https://local.dev-stack/crawler)`
  User-Agent and the `dev-stack-crawlutron` token are replaced
  with `DEFAULT_USER_AGENT` and `USER_AGENT_TOKEN` constants sourced
  from `app.constants`.
- **`worcesterma.gov` defaults replaced** (scripts/probe_session.py,
  scripts/e2e_verify.py): the `CRAWLUTRON_PROBE_TARGET` default
  is now `https://example.com/`.
- **Webhook delivery paths consolidated** (app/main.py): the
  IP-pinned delivery logic now lives in `_deliver_pinned_webhook`
  and is shared by both `_post_webhook` and `_run_and_deliver_webhook`,
  closing the TOCTOU window between endpoint validation and
  delivery.
- **`_PENDING_CLIENT_CLOSE` lock released before close** (app/crawl/
  strategies/http.py): the synchronous `client.close()` call on the
  dropped oldest client now runs outside `_PENDING_CLIENT_CLOSE_LOCK`
  so the close path cannot block other callers.
- **Cookie-namespace default documented** (app/crawl/strategies/
  http.py): the docstring on `_COOKIE_NAMESPACE` now states the
  multi-tenant opt-in requirement explicitly so silent cross-tenant
  cookie leakage is harder to cargo-cult.

### Round 7 hardening (adversarial review + jscpd duplication audit)
- **Frontier carries only validated URLs (H1)** (app/api/job_runner.py,
  app/api/response_shaping.py): whole-site job expansion enqueued the raw
  discovered `url` href while the SafeUrl-validated `normalized_url` sat
  unused one field away, so the frontier and the visited dedupe keyed on
  different strings for the same page. New `frontier_link_url()` chokepoint
  returns only the validated field; an AST drift detector bans raw-href
  frontier sinks tree-wide.
- **Pinned-URL unification (H4/H5)** (SafeUrl.connect()): every
  pinned-IP composition (HTTP fetcher, document extractor, webhook
  delivery) routes through one chokepoint with IPv6 bracketing and
  per-hop re-validation; `final_url`/cookies report the logical URL, not
  the pinned IP. Drift invariant requires every `SafeUrl.parse` site to
  commit to a `pin` decision.
- **Env-core consolidation (H1-OSS)**: all `CRAWLUTRON_*` numeric
  reads (including the 25 in `CrawlerSettings.from_mapping`) flow through
  `get_env_int/float`/`get_mapping_int/float` — bad strings can never
  reach arithmetic; the bool parser fails closed.
- **Capped webhook response drain (M3/L4)**: webhook delivery drains via
  `read_capped_async` (10 MiB) instead of unbuffered `client.post` bodies.
- **Single scope matcher (M10)**: engine/recursive's duplicated
  `_scope_allows` deduped into `app/crawl/scope_match.py`; one definition
  enforced by drift test.
- **Result shaping cannot drift**: every
  `UnifiedCrawlResponse`-with-results path (async runner, webhooks, both
  crawl_endpoint branches, render, MCP `tools/call`) now flows through
  `build_unified_crawl_response`; REST, job, webhook, and MCP payloads
  cannot diverge. The webhooks DI seam shrank 7 to 5 globals —
  shaper hooks existed only to be bypassed.
- **ContentCache mirror twins collapsed**: one locked probe and
  one locked store replace four hand-copied blocks. The twins had been
  hiding three real bugs: `aget` cold reads skipped the RAM-mirror entry
  cap (unbounded growth), `aset` mirrored before the disk write so a
  failed write left a stale mirror serving an unpersisted value, and the
  sync `get` never warmed the mirror the async path populated.
- **Error envelope factory**: 18 hand-rolled
  `{code, message, details}` dicts across 8 endpoint files replaced by
  `error_envelope`/`http_error`. Wire-visible effect: three error bodies
  that silently omitted `details` now always carry it (contract already
  documented it); the triplicated browser-pool 503 message is one
  constant. Envelope literals outside the sanctioned module fail CI.
- **One search-result normalization contract**: the
  Tavily/Brave/Serper parse loops — verbatim copies differing only in key
  names — collapsed into `_collect_results`. Behavior byte-identical; the
  risk retired is one provider's SSRF-drop or dedupe rule silently
  drifting from its siblings. Tree-wide detector keeps the loop in one
  place.
- **One robots.txt line-scanner**: crawl-delay, rules, and
  sitemaps each re-rolled the BOM/comment/`partition(":")` walk plus the
  RFC-9309 group accumulator, and had already drifted on the empty-value
  gate. `app/crawl/robots_parse.py` is the single tokenizer + selector;
  consumers supply only their "which directives do I accept" predicate.
  Shipped selection semantics (matched-group-is-final, invalid-delay falls
  back to star, agents accumulate across non-consumed lines) pinned by
  characterization tests. Detector bans a second `lstrip(BOM)+splitlines()`
  scan.
- **One sanctioned fetch fake (jscpd test-side)**: the 25 hand-built
  `HttpFetchResult(...)` fixtures across 14 test files now come from
  `_testkit.make_fetch`, which grew `headers`/`decompression_failed`
  passthroughs. No assertion changed; the win is that the next contract
  field lands in one builder instead of rippling through every fixture.
  Detector fails CI on a test that re-inlines the dataclass.
- **Decompression cap is mandatory (H2 closure)** (app/crawl/
  decompression.py, app/crawl/strategies/http.py): `max_output_bytes` was
  an optional keyword and three production paths called it without one —
  the uncapped `gzip.decompress`/`zlib.decompress`/`brotli.decompress`
  branches let a compressed bomb expand unbounded. The cap is now a
  required keyword and the uncapped branches are deleted; the defensive
  re-decompress in the HTTP strategy and the robots discovery read pass
  the policy cap and `DISCOVERY_BODY_MAX_BYTES` respectively. `_decode_body`
  lost its headers-triggered internal decompression entirely — decoding is
  downstream of the chokepoint, not a second decompression seam. AST
  detector bans any production decompress call that omits the bound.
- **One handle-minting primitive (H4 residue)** (app/crawl/ids.py):
  `new_request_id()`/`new_job_id()` replace the four re-inlined
  `uuid4().hex[:12]` mints (engine, crawl_endpoint, render) and job_store's
  raw hex. The 12-hex handles are the *only* lookup credential for
  `GET /v1/crawl/{request_id}`, so HIGH #9's "avoid brute-forceable short
  IDs" policy now governs them: 48 → 128 bits of entropy on the wire.
  The `crawl_` prefix and shape are unchanged, so no test assertions moved.
  AST detector bans any `uuid4()` call or `.hex` slice outside `ids.py`.
- **Batch-level failure accounting (job_runner)**: the per-item `failed`
  counter is cumulative across a batch, but its stats merge ran inside the
  loop — every item re-added the running total, inflating `total_failed`
  quadratically (2 failures reported as 3). The merge moved outside the
  loop; a two-failure execution test pins the count.
- **Await-in-lock detector stops being name-shy** (tests/
  test_safety_invariants.py): the M3 sweep matched only names containing
  "lock", so `self._mutex = threading.RLock()` — and the uppercase
  `_HTTP_CLIENT_INIT_LOCK`/`_PENDING_CLIENT_CLOSE_LOCK` in the HTTP
  strategy — escaped it. Matching is now case-insensitive stems
  (lock/mutex/guard) plus any name bound to a threading lock-family
  constructor in the module; bite test covers renamed, bound, and
  false-positive directions.
- **Env alias escape closed (H1 family)** (app/resource_pressure.py,
  tests/test_env_regression.py): `derive_browser_pool_size` read
  `CRAWLUTRON_BROWSER_POOL_SIZE` raw and wrapped `int()` in a
  try/except two statements after the `.get()` — invisible to the
  coercion-shape detector. The value now comes through `get_env_int`, so
  a non-positive or blank override falls through to derivation instead of
  clamping to 1 (pinned by parametrized behavior tests; garbage now also
  logs). The detector gains shape 3: any `int`/`float` of a name assigned
  directly from `os.environ.get`/`getenv`/subscript, bite-proven.

### Round 8 hardening (codebase-review fix stream)
- **Sync `get()` honors the mirror lock contract (M1)**
  (app/crawl/content_cache.py): `get()` warmed the RAM mirror outside
  `self._lock` while `aget()` held it, so the multi-step mirror/index store
  raced concurrent writers. The store now runs under the lock on both paths;
  a regression test pins the contract.
- **One failed item counts once (M3)** (app/api/job_runner.py): an item that
  failed `build_unified_result` AND carried `status=="failed"` incremented
  the batch `failed` counter twice. The status-failed count now applies only
  to built items; a dropped item counts exactly once either way.
- **Dead code in `BoundedTTLCache.get_stale` (H3)** (app/bounded_ttl.py):
  a stale `peek` docstring + `return` sat unreachable after the method's real
  returns (the file always parsed — severity was hygiene, not HIGH). Deleted.
- **Bounded cookie jar (R1)** (app/crawl/strategies/http.py): the
  module-global jar never evicted per-`(namespace, host)` slots. Storage is
  now an `OrderedDict` with an LRU cap (`_COOKIE_JAR_MAX_HOSTS = 1024`,
  refreshed on write and read); regression tests pin the bound and the
  read-refresh recency.
- **Batch URLs dedupe exact duplicates (wire-visible)**
  (app/crawl/payload_helpers.py): `_payload_urls` returned the list as-is,
  so identical URLs in one request each ran the full engine. Exact-duplicate
  strings now collapse to their first occurrence (order-preserving) at the
  chokepoint both `crawl_request` and `render_request` share, and the
  batch-size check counts unique URLs. Wire effect: a duplicated URL yields
  one result instead of N. The two render tests asserting the old
  no-dedup counts were updated to the new contract.
- **O(1) frontier membership (M2)** (app/api/job_runner.py): dedup scanned
  the persisted frontier list per link (quadratic on wide crawls). A
  transient membership set mirrors the list — rebuilt on first use after
  every store load, synced by `_pop_frontier`/`_frontier_add` — while the
  list stays the crash-safe source of truth. A 25-link fan-out test pins the
  multi-batch contract.
- **Map reads links through `frontier_link_url` (L1)** (app/map.py,
  app/api/response_shaping.py): the site-map had its own link-URL reader
  with a raw-href fallback. It now uses the validated-`normalized_url`
  chokepoint (widened to attribute-held `DiscoveredLink` dataclasses — same
  validated field, raw href never consulted). Existing map/job/MCP tests
  pin the preserved shapes.
- **Shared test drive mechanics (jscpd test-side)** (tests/_testkit.py,
  tests/api/): the `/v1/crawl` payload builder and the drive-a-job-to-
  settled prologue were hand-copied per file. New `_testkit.make_crawl_payload`
  and a local `_run_job_to_completed` prologue own them; call sites pass
  only their intentional variants. Zero assertions changed.
- **Remaining test-side duplicates consolidated (jscpd)** (tests/):
  per-file setup factories folded into one home each — `_testkit.pinned_renderer`
  (Host-override navigation), `_testkit.provider_results` (keyed-provider
  response bodies), `_testkit.extract_function_body` (source-fingerprint
  assertions); local helpers dedupe the webhook delivery DNS-pin prologue and
  the MCP `_tool_call` invocation; LRU/recursion/scope/admission/extraction
  test prologues share one builder. One clone pair is kept by design:
  the `render_router`/`search/router` “pure router” boilerplate is the
  idiomatic FastAPI file shape — a shared factory would obscure the trivial
  wiring, so the convention stays.

### Round 9 hardening (adversarial review)
- **Bound the robots Crawl-delay politeness floor (M1)**
  (app/crawl/robots_parse.py, app/scheduler.py): a hostile robots.txt could
  return `Crawl-delay: 1e999` (overflows to `inf`), which was stored
  uncapped and turned into `asyncio.sleep(inf)` **outside** the per-URL
  budget — hanging the whole scheduler gather. The parser now rejects
  non-finite values (a parse-level rule), and `set_domain_min_delay` clamps
  finite values to the existing politeness ceiling (`_MAX_COOLDOWN_WAIT`)
  at the write — the chokepoint where a delay becomes a scheduler wait. The
  ceiling lives only in the scheduler; the parser never clamps. A tree-wide AST
  invariant (`test_domain_politeness_floor_is_committed_only_via_the_setter`)
  fails CI if `_domain_min_delay` is ever written outside the clamped setter.
- **One keyed-provider constructor (jscpd)** (app/search/providers.py):
  Tavily/Brave/Serper each re-inlined `__init__` storing `api_key`. A shared
  `_ApiKeyProvider` base owns the credential slot; a contract test pins that
  each provider transmits the key it was built with.
- **One sanctioned test poller (jscpd test-side)** (tests/_testkit.py,
  tests/api/test_async_crawl.py, tests/test_safety_invariants.py):
  `poll_until` is the single predicate poller — the hand-rolled
  `while True` + `asyncio.wait_for` loop in `test_async_crawl.py` returned
  a bare bool on timeout instead of failing with the condition named, so
  every re-inlined copy silently diverges from the sanctioned
  timeout/fail contract. The copy is migrated to
  `poll_until(..., message=...)`; a tree-wide detector bans the shape in
  tests (only a real `pytest.fail` call node counts — a docstring claim
   does not), bite-proven in both directions.
- **Ceiling on explicit `max_content_length_bytes` (M2)**
  (app/constants.py, app/crawl/payload_helpers.py, app/api_models.py): an
  oversized per-page cap (e.g. `10**12`) was honored verbatim by
  `_effective_max_content_length`, disabling the RAM-budget scaling AND
  inflating the decompression bound — two chokepoints defeated by one caller
  field. `MAX_CONTENT_LENGTH_BYTES` (1 GiB) is now the hard ceiling: rejected
  at policy normalization (mirroring the existing `max_rps` bound, never
  silently clamped) and rejected at the pydantic boundary via `le=` (422).
- **Exact byte accounting on a failed `aset` write (L1)**
  (app/crawl/content_cache.py): the failure rollback mutated `_index_total_bytes`
  with a delta that had never been applied (bytes enter the total only at
  commit) — a new key's `total -= size` subtracted uncounted bytes (a `max(0,`
  `…)` clamp hid it only on an empty cache) and an existing key's
  `+= prior_size - size` drove the total to a negative. Since `_write_data` is
  `atomic_write_text` (prior file byte-identical on failure) and nothing
  mutates index/sizes/total/mirror before the write, the rollback is reduced to
  `raise`, and the commit now re-reads `old_size` under the lock — race-safe,
  matching sync `set()`. Two new tests pin `total == sum(sizes)` across a
  failed new-key and existing-key write.
- **Hostile `Content-Length` can't crash a fetch (L2)**
  (app/crawl/strategies/http.py): both fetchers pre-checked the declared body
  size with a bare `int(content_length)`, so a crafted `Content-Length: abc`
  raised out of the fetch and turned one response into that URL's failure. A
  single `_declared_length_exceeds` chokepoint now parses it, treating
  malformed/negative/absent as "unknown size" (skip the pre-flight; the
  streamed running cap still bounds the body). The scripted httpx mock
  transport moved to `_testkit` (dedup). A tree-wide AST invariant
  (`test_header_numbers_are_converted_under_guard`) fails CI on any unguarded
  `int()`/`float()` of a header-derived value, exempting conversion sites
  inside `try/except ValueError` — so the same class can't silently reappear
  at a new fetcher.
- **Async batch size rejected at the boundary (L4)**
  (app/api/crawl_endpoint.py, app/crawl/payload_helpers.py, app/crawl/engine.py,
  app/api/render.py): the async crawl path returned 202 before checking
  `max_batch_size`, so an oversized batch was silently accepted and only
  failed inside the detached background task — the caller could never see
  why. A `checked_payload_urls` chokepoint (dedup + cap in one call) now
  validates before the 202 in the endpoint and inside `execute_crawl`, so
   sync and async share one gate and cannot drift on batch admission.
- **Bounded retry backoff in `_fetch_with_retries` (L3)**
  (app/crawl/strategies/http.py): a failing fetch retried back-to-back with no
  wait, hammering an origin that is down or rate-limiting. Now `_retry_backoff_seconds`
  inserts a capped exponential gap (base 0.25s, doubling, ceiling 2.0s) between
   attempts, so the retry budget widens gaps instead of busy-looping, and a large
   `max_retries` can never park a URL past the per-retry ceiling.
 - **Two new same-day drift detectors** (tests/test_safety_invariants.py), closing
   enforcement gaps rather than just the bugs:
   - `test_locked_cache_helpers_are_called_under_the_lock` — any `_*_locked` cache
     helper must be invoked inside a `with …lock` block. The twin detector only
     caught raw `_memory_cache` access, so the M1 class (`get()` calling
     `_remember_memory_locked` off-lock while `aget()` held it) slipped past; this
     makes an unlocked `_*_locked` call a tree-wide failure. Non-vacuously sees all
     nine real call sites in `content_cache.py`, all correctly guarded.
   - `test_decompression_reaches_only_through_the_chokepoint` — no `app` module may
     import or call `gzip`/`zlib`/`brotli` directly; body decoding must route through
     the capped `decompress_body` chokepoint. The existing cap sweep guards the
     chokepoint's callers; this guards the bypass around it (a re-introduced
      header-trusting, uncapped decompress). Both ship `*_bites` proofs.
- **Page-identity dedup at the batch chokepoint ("never crawl the same page
  twice")** (app/crawl/payload_helpers.py): `_payload_urls` collapsed only *exact*
  duplicate strings, so a request carrying both `.../x` and `.../x/?utm_source=tw`
  — the same page under `normalize_public_http_url`, the service's declared single
  source of truth for page identity — still crawled twice. The chokepoint now dedups
  on the canonical key: fragment / trailing-slash / tracking-param / param-order
  variants collapse, first occurrence wins, order preserved — matching the identity
  already used by the visited set, the frontier, and the content-cache key. An
  un-canonicalizable value (non-http, or a private target under the SSRF guard)
  keeps its raw key so it stays a distinct entry and request-target validation
  remains the caller's job. A tree-wide AST invariant
  (`test_batch_dedup_keys_on_canonical_page_identity`) fails CI on any `dict.fromkeys`
   batch dedup that doesn't route through the canonicalizer; it ships a `*_bites`
   proof and is non-vacuous (`dict.fromkeys` appears nowhere else in `app/`).
 - **Self-describing contract: latency, async, and the dedup guarantee are now
   advertised** (app/discovery/{capabilities,agent_hints,well_known}.py,
   app/api/crawl_router.py): `/v1/capabilities` previously omitted the one real time
   ceiling (`total_url_budget_seconds`) and never mentioned `async_mode`, so a caller
   sizing an integration had no machine-readable way to learn that a synchronous
   `/v1/crawl` has **no server-side time ceiling** (duration scales with `max_pages`,
   depth, politeness delays, and the per-URL budget). Capabilities now advertise
   `default_limits.total_url_budget_seconds`, a `features.async_mode` flag, and a
   `crawl_timing` block (`per_url_budget_seconds`, `sync_has_server_time_ceiling`, and
   a `large_crawl_guidance` pointing to `async_mode` + `status_url` polling / `POST
   /v1/jobs` for long crawls). `/v1/agent-hints` gains async + page-identity guidance;
   `/.well-known/crawler-service` links `/v1/map`, `/v1/jobs`, and
   `GET /v1/crawl/{request_id}`. The `/v1/crawl` OpenAPI description was corrected the
   same way (its `202` said "webhook" only, now "`async_mode` or `webhook_url`").
   Doc fidelity: the README and `crawlutron-agent-contract.md` still claimed
   "callers own dedupe" and "5 MB → 5 MB" for the gzip-bomb test — both corrected to
   the real behavior (canonical within-request dedup + within-TTL reuse; a ~1 KB →
   1 MB gzip bomb plus deflate/brotli cases). Contract tests assert the new
    advertised fields.
 - **Durable whole-site jobs no longer lose the in-flight batch on a mid-crawl
   crash** (app/api/job_runner.py, app/crawl/job_store.py): exercising restart
   behaviour exposed that `_run_job` write-aheads the frontier pop (so the
   reload-each-iteration loop can't refetch a batch) but records results/
   `visited` only *after* the ~engine call, so a process killed mid-batch left
   the batch neither visited nor queued — on recovery `_pop_frontier` returned
   empty and the job was marked `COMPLETED` with **0 pages**. The persisted
   `CrawlJobState` now carries an `in_flight` list (schema `+ migration`, set in
   the same write-ahead save that removes the batch and cleared when its results
   commit); `_run_job` re-queues any non-empty `in_flight` onto the frontier at
   entry, so a crash resumes with at-least-once delivery (the `visited` set and
   content cache keep the re-fetch cheap and non-duplicating). This closes the
   #1834 "loses only in-flight work" claim — previously it also *dropped* it and
   falsely completed. A `TestCrashRecovery` test encodes the exact persisted
    post-pop/pre-record state and asserts recovery re-crawls it (the detection
    gap: nothing previously asserted in-flight durability).
  - **Transient failures no longer drain a whole-site frontier** (app/api/job_runner.py,
    app/crawl/job_store.py, app/constants.py): the runner marked *every* popped URL
    visited unconditionally, so a page that failed for a *transient* reason
    (network/dns/tls/timeout/browser crash — the long-dead but now-live
    `TRANSIENT_CLASSIFICATIONS`) was counted done and never re-queued. A whole-site
    job whose links fail during a network outage therefore drained to a seed-only
    `COMPLETED` — the exact 185 one-page towns from the 351-municipality exercise.
    `_run_job` now re-queues a transiently-failed URL (and a popped URL with no
    returned item) to the back of the frontier instead of visiting it, bounded by
    `MAX_URL_TRANSIENT_ATTEMPTS` (3) via a persisted `transient_retries` map so a
    permanently-dead page can't spin forever — on exhaustion it is recorded terminal
    with `error="retries_exhausted"`. Permanent failures (`not_found`/`site_error`/
    `gone`) stay terminal-once: a known-missing page is an answer, not a blip. New
    tree-wide detector `test_whole_site_visited_commit_is_transient_gated` forbids any
    `job.visited.add()` not gated on `TRANSIENT_CLASSIFICATIONS` (the local per-request
    `visited` set is deliberately out of scope), with a paired `*_bites` test.
  - **Sync `/v1/crawl` halts when the client disconnects** (app/api/crawl_endpoint.py,
    app/api/crawl_router.py): FastAPI does not cancel a running handler on disconnect,
    so an abandoned sync crawl ran to completion to feed a result nobody would read
    (the 283-abandon shape). The sync branch now races the engine call against
    `request.is_disconnected()` and cancels the crawl, letting the engine's existing
    cancellation cleanup free the slot/browser; a 499 `client_disconnected` is raised
    instead of a phantom 200. Covered by `tests/api/test_sync_disconnect_halt.py`
    (cancel-on-disconnect, result passthrough, error propagation; `real_sleeps`
    because the watcher depends on genuine loop yielding).

### Chokepoint hardening (bug-closure invariants)
- **A raising strategy is a failed STEP, not a dead URL**
  (app/crawl/strategy_chain.py): the HTTP strategy has its own broad-except
  sink and the browser worker catches inside `render`, but a raise at the
  adapter boundary between them escaped `execute_crawl_chain` and was
  enveloped by whatever ran the URL — with no strategy attribution and every
  remaining ladder step skipped, so one `BROWSER_HEADLESS` binding error
  ended a URL that still had `BROWSER_HEADED_WARMED` to try. `_execute_strategy`
  now converts any raise into a classified failed result through
  `_engine_result_from_exception`, and both `_run_*` helpers **and** the cffi
  cookie retry route through it, so a raise has exactly one place it can
  become a result. Wire-visible: a URL that used to come back as a generic
  unclassified envelope now reports the strategy that failed and its
  classification (`attempted_strategies`), and a ladder step after it can still
  succeed. Tree-wide AST invariant
  (`test_every_strategy_runner_call_is_guarded`) fails CI if a strategy runner
  is ever invoked outside a `try`/`except Exception` — a narrow handler does
  not satisfy it — with a paired bites case.
- **One scope-regex compiler, one length bound** (app/crawl/admission.py,
  app/crawl/engine.py): "compile a caller-supplied `scope_regex`, refuse a
  pathological one, bound the length" had two implementations — a public
  `compile_scope_pattern` in admission and a private
  `_compile_scope_regex_safe` in engine — that disagreed in their `except`
  clause (engine also caught `re.error`), behind a second module constant of
  the same name and value. Admission keeps the definition and now carries the
  union of both arms plus the N-1 rationale, so the crawl path and the durable
  admission path cannot compile different things from one policy. Pinning
  `test_scope_regex_compiler_has_exactly_one_definition` counts one compiler
  **and** one bound, tree-wide. Behavior for a caller is unchanged: a
  pathological pattern is still refused and a normal one still compiles.
- **Dead weight the invariants exposed** (app/scheduler.py, app/models.py,
  app/crawl/job_store.py, app/crawl/migrations/manifest.py,
  app/crawl/admission.py, app/api/render.py): `job_store.from_json` branched on
  a `canonicalized` flag hardcoded `False` whose column migration `0002` had
  already dropped, and three docstrings described the dropped marker as if it
  still existed; `_row_is_canonical` was the same removed flag's other half and
  nothing called it. `DomainScheduler._job_url` had no caller, and `run`'s
  `url_of` parameter existed only to be ignored — the render pipeline was
  passing an argument that silently did nothing. `_is_classification` had been
  replaced by the `_CLASSIFICATION_TABLE` lookup below it (its docstring still
  claimed otherwise), `create_index_statements` had no caller and no schema
  declaring an index, and `RobotsGate._cache` was assigned and never read.
  `app/` is 50 lines smaller and the whole-tree line ceiling came with it. No
  behavior change: every removal was verified unreferenced across `app/`,
  `tests/`, `scripts/`, and `docs/` first (a naive private-name scan is
  worthless here — string-literal `cast()` targets and FastAPI route functions
  both read as unreferenced and neither is dead).
- **No store method runs on the event loop** (app/crawl/job_store.py,
  app/crawl/result_store.py, app/api/jobs_router.py, app/api/job_runner.py,
  app/api/async_crawl_runner.py, app/api/crawl_endpoint.py): `JobStore` and
  `CrawlResultStore` are synchronous by design (SQLite plus `os.fsync`), and
  every async caller was reaching them directly — a `.save()` of a large job
  row blocked the whole loop, one request at a time. Each public method now has
  an `a`-prefixed twin that offloads through `asyncio.to_thread`, and every
  async frame calls the twin. The sync methods stay for the tests and for the
  thread-affine lifecycle (`close`, reset); they are not deleted, so the ratchet
  gain here is behavioral rather than cosmetic. Tree-wide AST invariant
  (`test_no_sync_store_method_is_called_from_an_async_frame`) fails CI if a sync
  store method is ever called from an `async def`, with a paired bites case; and
  four loop-guard tests drive real store operations on a loop and fail loudly if
  the operation ever runs on it, so the twins cannot silently become no-ops.
- **A callback failure is not a worker failure** (app/scheduler.py,
  tests/crawl/test_scheduler.py): `on_result` was awaited inside the worker's
  own `try`/`except`, which made a raising result-callback report as a failed
  *fetch* and vice versa — two unrelated failure classes sharing one log line
  and one status field. The callback now runs outside the worker's handler (it
  still runs exactly once, on a slot the fan-in has already released, so a
  callback storm cannot starve live work), and its own failure is logged
  separately instead of vanishing into a retried crawl. Tree-wide drift test
  (`test_a_worker_call_on_result_is_guarded_by_its_own_handler`) fails if the
  two are ever merged back, with a paired bites case.
- **HTTP pool defaults are derived, not frozen at import**
  (app/settings.py, tests/test_crawler_settings_defaults.py): the two httpx pool
  fields called their derivation as a default value in the dataclass body, so
  they were evaluated once — at import — and the cgroup memory read behind them
  (with its 4 GiB fallback on failure) was frozen for the process lifetime. A
  process whose memory limit only became readable after import, or that hit the
  fallback, then ran a pool sized from a number nothing in the config could
  explain. Both fields are now `field(default_factory=...)`, so every
  construction re-derives, and `from_mapping` can no longer read a stale
  class-level default. Shipped numbers are unchanged in the normal case.
- **A wedged action chain releases the slot at the render budget, and says so**
  (app/crawl/strategies/browser.py, tests/crawl/test_browser_cookie_harvest.py):
  `_run_action_chain` was bounded by the selector timeout alone, but a
  `page.hover` or `page.wait_for_function` that never returns is not a selector
  wait, so a hostile page could hold a pooled browser slot far past the timeout
  that was supposed to bound it. The chain now runs under the render budget as
  one bounded hold, and the per-action failures it already swallowed (each
  best-effort action continuing to the next) are logged with the URL instead of
  being invisible — a selector timeout, in particular, is now identifiable
  because the log names the URL it was waiting on.
- **A worker of the wrong shape is named at the seam** (app/crawl/worker.py,
  tests/crawl/test_error_handling.py): `call_worker` dispatches on whether the
  worker is a coroutine function, so a plain function that RETURNS a coroutine
  (the shape `lambda job: render(job.url)` produces) took the sync branch and
  handed the strategy an un-awaited coroutine. The browser strategy then read
  `.html` off it and reported a `network_error` — which reads like a site
  problem and points the investigation at the page instead of at the double.
  A sync worker returning an awaitable now raises `TypeError` naming the worker.
  Found by the full sweep, which un-deselects the `browser`-marked tests the
  default `addopts` skips: one of them had been failing on this since before it
  was marked opt-in.
### Deployment posture
- **Zero-telemetry invariant** (Dockerfile runtime ENV + docker-compose.yml):
  `ORT_DISABLE_TELEMETRY=1` disables onnxruntime's native client telemetry (via
  magika) at lib load, and the Microsoft OneCollector / App-Insights host family
  is sinkholed to `0.0.0.0` via `extra_hosts` as the un-bypassable egress
  chokepoint for libraries with no opt-out flag (Playwright's driver ships none
  in this version; camoufox phone-homes only on explicit operator action).
  Posture and verification documented in
  docs/references/crawlutron-telemetry.md.

### Repository scaffolding
- Added `pyproject.toml`, `LICENSE` (MIT), `.env.example`, `SECURITY.md`,
  `CONTRIBUTING.md`, `.github/workflows/ci.yml`, `Makefile`,
  `docker/browser-crawler/scripts/run-tests.sh`.
- `requirements.txt` now lists `lxml`, `beautifulsoup4`, `markdownify`
  (previously silent dependencies).
- `scripts/probe_session.py` no longer hardcodes `/opt/stack/...`
  paths; target and output are env-var configurable.

### Test infrastructure
- `./test.sh crawlutron` (dev-stack root) now delegates to
  `docker/browser-crawler/scripts/run-tests.sh` instead of failing on
  missing `tests.test_crawlutron_api` / `tests.test_crawlutron_core`.
- New `tests/crawl/test_cookie_namespace.py` covers the per-request
  cookie namespace behavior (7 tests).
- Test suite: **738 passed, 54 browser-marked deselected** in ~32 s.
- Added `tests/test_adversarial_round3_fixes.py` (7 tests) covering
  round-3 reviewer findings (sync `close()` on the dropped client,
  `Connection: close` on truncated requests, per-chunk trailers
  preserved across truncation, etc.).

## [0.1.0] - 2026-08-21

Initial extraction from `docker/browser-crawler` (dev-stack monorepo).
Captured the state at the close of the first adversarial review
(`ADVERSARIAL_REVIEW_BROWSER_CRAWLER.md`). All 7 prior findings (ReDoS, raw exception
text, local-mode SSRF misroute, robots regex recompile, httpx client
leak, JobStore concurrency, selector DoS) were already fixed.


## [0.3.0] - Open-Source Extraction Hardening

### Changed
- **Removed monorepo entanglements**: ``local_service_logging`` shim
  promoted to a standalone ``app/logging_config.py``. The
  ``shared/python`` path injection in ``tests/conftest.py`` and
  ``scripts/probe_session.py`` is gone. ``app/main.py`` no longer
  references any path outside the package itself.
- **Portable defaults**: ``app/settings.py`` defaults changed from
  ``/app/logs/...``, ``/app/cache`` to ``./logs/...``, ``./cache`` so
  the package works on a fresh checkout without a specific mount
  layout. The runtime now creates those directories at lifespan
  startup if they are missing.
- **Identity strings**: ``DEFAULT_USER_AGENT`` and ``USER_AGENT_TOKEN``
  constants added to ``app/constants.py``. All references to the
  dev-stack monorepo / local hostname have been replaced with the
  project-neutral ``crawlutron/0.2`` identity. FastAPI
  ``contact`` and ``license_info`` now reference the project, not the
  monorepo.
- **Probe target neutral**: ``scripts/probe_session.py`` and
  ``scripts/e2e_verify.py`` default to ``https://example.com/``
  instead of a specific municipal domain.

### Security
- **SSRF env var parsing centralized**: ``app.validation.parse_bool_env``
  now owns the ``CRAWLUTRON_ALLOW_PRIVATE_TARGETS`` parsing with
  consistent ``1``/``true``/``yes``/``on`` semantics. The
  ``os.environ.get(...) == "1"`` check at three call sites has been
  replaced.
- **Webhook TOCTOU window closed**: ``_post_webhook`` and
  ``_run_and_deliver_webhook`` now both route through a single
  ``_deliver_pinned_webhook`` helper, so the SSRF validation and
  IP-pinning happens in one pass. A webhook that passes validation
  cannot have its DNS record flipped between the check and the
  delivery.
- **``_PENDING_CLIENT_CLOSE`` lock scoping**: the synchronous
  ``client.close()`` call now happens OUTSIDE the critical section
  (the lock is released first, then the blocking close runs). This
  prevents the close-from-no-event-loop path from stalling other
  threads waiting on the lock.

### Fixed
- **Classification input change on script-truncated bodies**: the linear HTML
  noise scanner neutralizes an unclosed ``<script>`` to end of input, so for
  bodies truncated mid-script by ``max_content_length`` the text after the
  cut no longer reaches classification (the old regex left truncated-script
  content visible). Deliberate fail-closed trade; classification-sensitive
  callers may see diffs on truncated pages.
- **``_ResultSpill.close()`` double-unlink**: removed the duplicate
  ``if self._own:`` block that could ``os.unlink`` the same path
  twice (raising ``FileNotFoundError`` once the first unlink
  succeeded).
- **Browser pool ``None`` guard**: a request that does not need any
  browser strategy (``HTTP_DECLARED_CRAWLER`` /
  ``HTTP_CFFI_IMPERSONATE`` only) no longer 503s when the browser
  pool is not initialized. URL validation still happens up front so
  invalid URLs surface as 422, not 503.

### Removed
- ``app/browser.py``: re-export shim for ``BrowserRenderer`` and
  related symbols. Canonical location is
  ``app.crawl.strategies.browser``.
- ``app/service.py``: re-export shim + the browser-only
  ``render_request`` helper. ``render_request`` moved to
  ``app/api/render.py``; everything else to ``app.crawl.engine`` /
  ``app.crawl.strategies.http`` / ``app.crawl.document_text`` /
  ``app.crawl.extraction`` (its canonical source).
- ``REVIEW_ROUND2.md``: 98-byte empty header, replaced by the
  successful ``REVIEW_ROUND3.md`` review.

### Build
- ``Dockerfile``: the circular ``COPY --from=crawlutron:latest``
  reference (which broke fresh-clone builds where the source image
  had not been tagged yet) replaced with ``COPY --from=camoufox-binary``.
  The ``shared/python/local_service_logging`` ``COPY`` is gone (the
  module is now in-tree).
- ``docker-compose.yml``: new standalone file at the repo root so the
  service can be run from a fresh clone without any parent
  ``docker-compose.yml`` / ``docker-compose.municipal.yml`` mounts.
- ``Makefile`` ``clean`` target: now uses ``-prune`` to walk leaf
  ``__pycache__`` directories first, avoiding the "directory vanished
  mid-rm" race that the previous flat ``-exec rm -rf`` could hit.

### Developer
- ``mypy.ini`` + ``pyrightconfig.json``: optional type-checker configs
  for users running stricter checks locally. Both default to "type
  checking basic" mode and ignore ``tests/`` / ``scripts/`` (test
  code uses ``from coverage_helpers import *`` patterns that confuse
  whole-file checks).
- ``ssrf_guard_required`` pytest fixture: tests that exercise
  ``_check_ssrf_protection`` or ``_resolve_and_pin_host`` can opt in
  via this fixture, which fails the test if the SSRF helper ran
  while ``CRAWLUTRON_ALLOW_PRIVATE_TARGETS`` was still enabled
  (the silent false-positive that lets the guard short-circuit
  before real test logic runs).


## [0.3.1] - Reviewer-5 Follow-Up

### Fixed (from `REVIEW_ROUND5.md`)
- **Dockerfile comment cleanup**: the `# crawlutron:latest` comment in
  the runtime stage now describes the historical reason for using
  `--from=camoufox-binary`, not an active broken reference.
- **`app/main.py`** has zero runtime `try/except ImportError` for the
  logging helper. The previous comment that referenced the deleted
  monorepo shim has been condensed.
- **Dockerfile** no longer references `shared/python/local_service_logging`
  by name in comments (still no `COPY` line for it).
- **`CONTRIBUTING.md`** no longer tells contributors to vendor
  `local_service_logging` from a parent repo that does not exist. The
  paragraph now points at `app/logging_config.configure_service_logging`
  as the in-tree equivalent.
- **`tests/conftest.py`**: the suite-wide default for
  `CRAWLUTRON_ALLOW_PRIVATE_TARGETS` is now gated behind a new
  `CRAWLUTRON_TEST_ALLOW_PRIVATE_TARGETS` env var. Defaulting to
  `"1"` (loopback-crawl-friendly) preserves all 740 existing tests.
  CI can set the new var to `"0"` for an SSRF-strict mode where every
  loopback test must explicitly opt in.
- **Webhook test race fixed**: `tests/api/test_crawl.py::test_v1_crawl_webhook_returns_202_and_delivers`
  now polls `WEBHOOK_RECEIVED` for up to 10s instead of asserting
  synchronously after the `POST /v1/crawl`. Polling helper moved to
  `tests/_testkit.py::poll_until` for reuse; `tests/api/test_async_crawl.py`
  imports it from there.
- **Stale docstring references to `app/service.py` and `app/browser.py`**
  removed from `app/constants.py`, `app/crawl/payload_helpers.py`, and
  `app/crawl/extraction.py`. The two shim files themselves were already
  deleted; only docstring breadcrumbs remained.

### Deferred
- Splitting `app/main.py` into routers (~900 lines, acceptable for
  initial release; tracked as P2).
- Replacing `_reset_global_http_state` fixture with DI/registry (P3).
- Auditing all `except Exception` catches (P3).


## [0.3.2] - Reviewer-6 Follow-Up

### Fixed (from `REVIEW_ROUND6.md` reviewer feedback)
- **`_parse_robots_crawl_delay`** uses `USER_AGENT_TOKEN` (was already
  fixed in the prior round but the reviewer noted it; verified twice).
- **SSRF test-suite default inverted**: `tests/conftest.py` now sets
  `CRAWLUTRON_ALLOW_PRIVATE_TARGETS` to `"0"` (the secure
  production default). A session-scoped autouse fixture
  (`_enable_loopback_for_test_suite`) flips it to `"1"` for the
  default test run so existing loopback-crawl tests keep working
  unchanged. CI runs that want SSRF-strict mode set
  `CRAWLUTRON_TEST_STRICT_SSRF=1` to disable the session-wide
  flip; every loopback test must then opt in explicitly via
  `monkeypatch.setenv("CRAWLUTRON_ALLOW_PRIVATE_TARGETS", "1")`.
- **`docker-compose.yml`** standalone file exists at the repo root
  (already created in round 4; verified).
- **`app/browser.py`** and **`app/service.py`** shims are deleted
  (already done; verified no callers remain and stale docstring
  references cleaned up).
- **`setuptools`** removed from runtime `requirements.txt` /
  `pyproject.toml` (already done; verified no references remain).
- **`mypy.ini`** + **`pyrightconfig.json`** present at the repo root
  (already added; verified).

### Changed (Reviewer-6 follow-up)
- **`CONTRIBUTING.md`** security-sensitive file list expanded to
  include `app/validation.py` (SSRF guard), `app/crawl/strategies/
  http.py` (DNS pinning / cookie isolation), `app/crawl/strategies/
  browser_pool.py` (browser-pool sizing), and `app/settings.py`
  (defaults are security decisions).
- **`README.md`** adds an "Adversarial Test Coverage" section
  highlighting the dedicated adversarial test suites (decompression
  bombs, DNS pinning, classification corroboration, etc.).

### Refactored (split out of `app/main.py`)
- **`app/api/webhooks.py`** owns all webhook delivery helpers
  (`validate_webhook_url`, `deliver_pinned_webhook`, `post_webhook`,
  `run_and_deliver_webhook`, `deliver_webhook`). Module-level state
  (semaphore, shutdown event, callbacks) is wired once at lifespan
  startup via `set_webhook_dependencies`; cleared on teardown.
  This removed ~110 lines from `app/main.py` and made the webhook
  helpers independently testable.

### Documentation
- **`_gc_stale_spills`** gained a docstring explaining the inode/
  fd lifecycle race window (Linux: unlink-then-close is safe; Windows:
  PermissionError is caught and the spill stays).
- **`_compile_scope_regex_safe`** quantifier lookbehind
  (`(?<!\\)` vs the reviewer's claimed `(?<!\\))`) now has an
  inline comment explaining why the form used is correct.
- **`_SITEMAP_CACHE`** has a thread-safety comment noting GIL
  protection under CPython and the migration path if the parsing
  moves to a thread.
- **`ContentCache.set()` → `_enforce_max_entries()`** lock-release
  window has an inline comment documenting the benign over-sized
  index window.

### Deferred
- **Split `app/main.py` into routers** — the `/v1/crawl` endpoint
  alone is ~200 lines and tightly coupled to the FastAPI app instance
  (background tasks, metrics, exception handlers, job store).
  Splitting cleanly requires a Router refactor with dependency
  injection for background tasks + metrics; tracked as P3.
- **Replace `_reset_global_http_state` fixture with DI/registry** —
  manually resets ~8 module-level singletons. Replace with a context
  manager that owns the lifetime of each singleton. Tracked as P3.
- **Audit all `except Exception` catches** — there are ~30 sites
  across `app/`. Most are best-effort teardown (browser, HTTP client,
  webhook) and correctly swallow; a few (sitemap parsing, classification)
  could log the swallowed exception to aid debugging. Tracked as P3.
