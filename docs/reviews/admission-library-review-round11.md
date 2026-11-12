# Adversarial review round 11 — `admission` v2.10 spec (final verification)

**Verdict: defects-found — one MINOR (B-1) on the R10-B1 sentence; R10-B2
RESOLVED, R10-B3 RESOLVED, rounds-1–9 sweep INTACT, greps CLEAN, changelog
PRESERVED.** Reviewer: ses_e313403dcffewaAkodzf9tEq3e. "One hair of precision;
no design inversion, no acceptance-row/normative split, no Model-A coherence
break. After that sentence is tightened, the document is the implementation
brief."

## B-1 (MINOR, §4.2 / R10-B1) — "ultimate bound" names the per-job timeout as
a bound on the LOOP; caller deadline not mechanically enforced

**Trace (verified against live tree):** each grant is a body = `_run_job` =
`wait_for(execute_crawl_chain(...), timeout=budget)`
(`request_pipeline.py:166-188`, `budget = total_url_budget_seconds` at :166,
TimeoutError → `Classification.url_budget_exceeded` at :195). So `budget` is
PER-JOB (per-grant); on elapse the frontier item is consumed and the next
grant re-admits with a FRESH budget. The job-level timeout bounds each JOB,
not the LOOP: a re-admitting zero-output loop has no per-budget wall-clock
bound; it terminates only when the frontier empties or `max_pages` is hit.
For a durable job with `max_pages=0` and a discovery stream refilling the
frontier, the loop has NO wall-clock bound in crawlutron today (no
`wait_for`/deadline wraps the whole `crawl_batches`; `CrawlPolicyLimits`
exposes only per-URL `total_url_budget_seconds` and per-strategy
`timeout_seconds`; `await_or_client_gone` cancels on client disconnect, not a
deadline). The sentence would encode a false guarantee into the implementer's
contract text.

**Minimal fix (one clause, exact replacement for the sentence's lead):**

> **Ultimate bound (R10-B1):** each granted job on a continuously-granting /
> continuously-alternating zero-output loop is bounded by the crawl's own
> per-job execution timeout (…same anchor…); the **loop** itself is bounded
> only by the finite work surface (frontier drain / `max_pages`) and whatever
> caller deadline the caller itself enforces — which the kernel, and
> crawlutron, do not provide — so a zero-output loop that keeps being granted
> work has no admission-machinery wall-clock bound.

Strengthens, not weakens, the existing R8-B1 "it cannot stop a loop that keeps
being granted work." The changelog's v2.10 paragraph mentions the per-job
timeout as a bound on the "continuously-granting … zero-output loop" too —
that mention must be re-scoped identically (per-job claim; loop bounded only
by finite work surface / caller-enforced deadline).

## B-2 (acceptable as written) — R10-B2 cross-reference is honest for the
lease case

The grant body is itself wrapped in `wait_for(timeout=budget)`, so it cannot
be held forever — a "permanently held" lease is released on the per-job body
timeout/cancellation. The cross-reference genuinely terminates the park for
crawlutron's own wiring; not a bare hypothetical. No edit required (shares
B-1's fact; B-1's fix is the authoritative home).

## B-3 (no tension) — R6-B1/R8-B1 vs R10-B1

The delegation split is precise: the admission machinery does not bound the
loop; separately, the per-job timeout + caller deadline bound each job / any
caller-provided loop deadline. No passage claims the machinery bounds it.

## B-4 (full-read coherence) — no additional defects

Acceptance rows consistent (row 8 gate-liveness vs §4.4; row 10
page_count-only vs §4.2/unit 8); Model-A coherence intact across 16 rounds
(door, repealed `Granted | Idle`, one wait site, no-TTL consistent); newest
sentences coherent as a system.

## Gate-readiness

One-hair fix on the R10-B1 sentence + changelog re-scope, text supplied.
Reviewer's explicit position: "with the proposed one-hair fix, the doc is the
implementation brief — the implementer can write the library from it with no
further design decisions."