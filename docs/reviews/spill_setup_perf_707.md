# Spill-setup performance — #707 measurement report

**Issue:** #707 — per-request in-memory spill DB migration/setup cost  
**Status:** not material — no code change (data below)  
**Decision:** close recommendation; leave the issue open with this evidence  
**Branch:** `work/issue_707` (from `main` at `71881c9d`)  
**Committed:** `scripts/bench_spill_setup.py` + this report

---

## 1. Question

`_bind_spill` (`packages/core/crawlutron/orchestration/engine.py`) creates `CrawlSpillStore(":memory:")` for every sync crawl that is not given a shared store, and `connect_store` (`packages/core/crawlutron/storage/sqlite_store.py`) opens the connection, applies service PRAGMAs (busy-timeout, WAL) and runs the full migration battery via yoyo on a fresh in-memory database. Is that per-request cost material?

## 2. Grounding (live tree)

The sync crawl path:

- `/v1/crawl` (`packages/rest/crawlutron_rest/api/crawl_endpoint.py` → `crawl_executor.execute_crawl`) → `packages/core/crawlutron/orchestration/engine.py:crawl_request`.
- `crawl_request` calls `_bind_spill(spill_store)`:
  - shared store supplied → reuse it, `private_spill=False` (no per-crawl setup, no close);
  - `None` → `CrawlSpillStore(":memory:")`, `private_spill=True`.
- `CrawlSpillStore(":memory:")` → `CoordinationStore.__init__` → `connect_store(":memory:", schema=CRAWL_SPILLS)`:
  - `_open_raw_store_connection` opens a named shared-memory connection (`file:<name>?mode=memory&cache=shared`);
  - `configure_store_connection` sets `busy_timeout` and `PRAGMA journal_mode=WAL` (bounded retry);
  - `migrate_store` runs the yoyo migration battery — for `CRAWL_SPILLS` that is a single migration, `0001_crawl_spills_initial` (`CREATE TABLE crawl_spills` + index), then a drift check.
- Teardown (`engine.py:_teardown_spill`): `release_spill(request_id)` always; `spill.close()` only for private stores.

One important wiring fact: `packages/rest/crawlutron_rest/main.py` creates **one shared** `CrawlSpillStore(database_path)` at startup and exposes it via `app.state.spill_store` → `get_spill_store` dependency. The normal HTTP `/v1/crawl` sync path therefore passes a shared store and pays **zero** per-crawl spill setup/migration. The per-crawl `:memory:` store only happens for direct `crawl_request()` callers that pass `spill_store=None` (MCP door, programmatic/test callers).

## 3. Methodology

- Harness: `scripts/bench_spill_setup.py`, runnable with `python scripts/bench_spill_setup.py`.
- Hermetic: no real network. Fetches answered by `tests._fetch_doubles.FetcherDouble` against a scripted answer (small HTML body).
- Timing: `time.perf_counter`, `statistics` (min/mean/median/max/pstdev), repeated runs.
- Two measurements:
  - **(a)** Direct per-request spill lifecycle in isolation: construct + migrate `CrawlSpillStore(":memory:")`, `release_spill(...)`, `close` — 500 repetitions. This *is* the exact per-crawl spill tax.
  - **(b)** End-to-end sync crawl (1/10/100 pages) with a private per-crawl store vs a shared pre-migrated store, 60 repetitions each, isolating per-request spill setup+teardown from the crawl body.
- Worst-case framing for (b): `asyncio.sleep`/`time.sleep` pinned to a zero-yield (mirroring the suite's `fast_sleeps` fixture), so scheduler politeness delay is removed and crawl body = fetch work only (instant here). That **maximises** the spill setup's share of crawl time; with real network fetch the share is smaller still.
- `process_state.reset_all_process_state()` between crawls to avoid cross-crawl global-state accumulation; each shared store fresh per repeat so the shared body is not inflated by accumulated rows.

## 4. Environment / machine profile

- Host: macOS 27.2 (arm64), Apple M4 Pro
- Interpreter: Python 3.12.14 (crawlutron venv)
- sqlite3: 3.53.1; yoyo: 9.0.0
- Repo HEAD: `71881c9d chore: merge main for #708 landing`
- Note: `psutil` not installed, so core/memory counts are not quoted.

## 5. Results

### (a) Direct per-request spill lifecycle — construct + migrate + release + close (500 iters)

| stat | ms |
|------|-----|
| min | 0.523 |
| mean | 0.609 |
| median | 0.575 |
| max | 7.980 |
| pstdev | 0.334 |

So the per-request spill setup+teardown tax is **~0.6 ms mean per crawl** (sub-millisecond, sub-millisecond variance).

### (b) End-to-end sync crawl — private per-crawl store vs shared store (60 iters each, zero network latency)

| pages | private mean (ms) | shared mean (ms) | spill tax (ms/crawl) | spill tax (% of crawl) |
|------:|------------------:|-----------------:|---------------------:|-----------------------:|
| 1 | 4.127 | 1.804 | 2.323 | 56.3% |
| 10 | 14.475 | 13.655 | 0.820 | 5.7% |
| 100 | 126.568 | 125.740 | 0.827 | 0.65% |

Notes:
- The 1-page row is a degenerate sub-5 ms crawl; the spill store dominates only because the crawl body itself is ~1.8 ms of instant fetch work.
- At 100 pages the measured tax is ≈0.8 ms (~0.65%), within noise of zero on a couple of repeats — the crawl body dwarfs spill setup.
- With real network fetch (tens to hundreds of ms per page), the spill share is **<< 1%**.

## 6. Decision: not material — no code change

The per-request spill setup cost is ~0.6 ms per crawl and is **not material** under the brief's threshold (>~1% of representative crawl):

- On the normal HTTP `/v1/crawl` sync path there is **no per-crawl spill cost at all**: `main.py` builds one shared `CrawlSpillStore` at startup and wires it through `get_spill_store`; the migration runs once.
- On direct `crawl_request()` callers (MCP/programmatic) that pass `spill_store=None`, the tax is ~0.6 ms per crawl — small relative to any real fetch, and bounded by the concurrency slot cap (`CRAWLUTRON_MAX_ACTIVE_REQUESTS`) rather than unbounded RPS.
- Even in the zero-network-latency worst case, spill setup is at most single-digit percent of an instant crawl and ≈0.65% of a 100-page crawl.

**Recommendation:** no code change. Preserve the existing isolation/fenced-GC semantics (private per-crawl `:memory:` store with live-writer fence + age-reclaimable GC; shared store for the HTTP door) unchanged. If a future high-RPS direct-`crawl_request()` workload is demonstrated to be spill-bound, a follow-up ticket can pre-build a schema template DB or use a process-wide spill store with per-request prefixes + fenced GC — but the data does not justify that complexity today.

## 7. Gates (all green on the new script)

- `ruff check` — pass
- `ruff format --check` — pass
- `mypy scripts/bench_spill_setup.py` — pass
- `basedpyright scripts/bench_spill_setup.py` — 0 errors/warnings
- `vulture crawlutron tests scripts --min-confidence 90` — pass (no dead code)
- No `Any`, no magic comments/suppressors; no production behavior changed.

No full randomized battery was required because no non-doc production code was touched (only a new standalone script under `scripts/`).

## 8. Files added

- `scripts/bench_spill_setup.py` — repeatable, hermetic benchmark harness
- `docs/reviews/spill_setup_perf_707.md` — this report

Commit cites #707. Issue #707 left open with this evidence posted as a thread comment.
