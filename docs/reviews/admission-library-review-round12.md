# Adversarial review round 12 — `admission` v2.11 spec (final verification)

**Verdict: ALL-CLEAR — the document is the implementation brief.** Reviewer:
ses_e3130503bfferQuFBuRD7F73J8. "The library implementer can write from this
document with no further design decisions." Seventeen review rounds deep.

## Part A — verified

- **R11-B1 RESOLVED**: §4.2 "Ultimate bound (R10-B1, scoped R11-B1)"
  (lines 510-523) carries the loop-vs-job re-scope; changelog v2.10 first
  sentence re-scoped identically. All four live claims verified: (a)
  `recursive.py:466-474` calls `crawl_batches` with no timeout wrapper; (b)
  `request_pipeline.py:166-195` wraps each granted job's `execute_crawl_chain`
  in `wait_for(timeout=budget)` — per-grant, fresh budget on re-admit,
  TimeoutError → `url_budget_exceeded`; (c) `CrawlPolicyLimits` (full read,
  lines 23-132) has NO whole-crawl deadline field; (d) `job_runner._should_stop`
  (:222-231) is purely event-driven (PAUSED/CANCELLED/shutdown_event), no
  wall-clock deadline.
- **Full rounds-1–10 regression sweep INTACT** — itemized, every resolution
  present and un-weakened.
- **Leftover greps CLEAN** — itemized.
- **Changelog-verbatim PRESERVED** — v2.1–v2.9 historical records coherent;
  v2.10 first sentence re-scoped per round-11 mandate; v2.11 supersedes.

## Part B — fresh attacks

- **B-1 (machinery-never-bounds-the-loop sweep) — NO DEFECT.** Every
  machinery-bound claim is per-park/per-grant/per-job, never loop-level; the
  three "no admission-machinery wall-clock bound" statements are unconditional
  and correct.
- **B-2 (finite-work-surface guarantee-vs-contingency) — MINOR,
  NON-BLOCKING.** The "finite work surface" limb presumes finiteness; the live
  loop (`while state.frontier and (max_pages <= 0 or state.page_count <
  max_pages)`, `admission_stall.py:114`) with `max_pages=0` + refilling
  frontier is unbounded. BUT the sentence is an exhaustive enumeration of
  possible bounds; the load-bearing unconditional term ("no
  admission-machinery wall-clock bound") is unambiguous and correct. Optional
  one-clause polish (recorded on issue #887 for the implementer, not applied):
  "when that surface is finite; if it is not, only a caller-enforced deadline
  bounds the loop". **Ruling: ship as-is.**
- **B-3 (R10-B2 cross-reference coherence) — NO DEFECT.** The dependent clause
  is byte-unchanged by v2.11; round-11 B-2 already adjudicated it "acceptable
  as written"; do not reopen.
- **B-4 (full-read coherence) — NO DEFECT.** Acceptance rows consistent;
  17-round Model-A coherence intact; quality bar met.

## Gate-readiness

**Document ships as the implementation brief.** No further design decisions
remain. The single optional clause (B-2) is recorded on the issue for the
implementer's first draft — not a hold.