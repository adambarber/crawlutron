# Adversarial review round 8 — `admission` v2.7 spec (final verification)

**Verdict: defects-found — two MAJOR, one MINOR; not yet the implementation
brief.** Reviewed by ses_e314240bfffebWJxaMX7lb64Qt against
`docs/design/admission-library-v2.md` (v2.7, 1077 lines) with live-tree
verification (`admission_stall.py:113-130`, `recursive.py:344`, inventory
recount). The round-7 BLOCKER is genuinely and completely resolved (Part A
all PASS: allowed_jobs exclusion coherent across 4.2/unit 8/row 10; migration
is a real code change byte-matched to the live cite; grep survivors all
legitimate; row-10 probe mutation-grade; full rounds-1–6 regression sweep
INTACT including a fresh 4+13 inventory recount; leftover greps clean). Three
fresh findings on the v2.7 surface.

## MAJOR — R8-B1: the delegation over-sells the page_count-only record as the
crash-loop detector; the real trip-wire is a zero-admission pass (§4.2:428-436
× §9 unit 8 × §10 row 10 × live `admission_stall.py:113-130`)

Live flow:
```
while state.frontier and (max_pages <= 0 or state.page_count < max_pages):   # 114
    page_count_before = state.page_count                                      # 117
    allowed_jobs = await stages.start_runner(run, state, metrics)             # 118
    if not allowed_jobs and stall_guard.stalled():                            # 119  <-- TRIP WIRE
        ... return (stop)
    ...
    if allowed_jobs or state.page_count > page_count_before:                  # 129
        stall_guard.record_progress()
```
`stalled()` is consulted ONLY when `not allowed_jobs` (line 119 short-circuits
on truthy `allowed_jobs`). The kernel's floor law keeps `allowed_jobs` truthy
in exactly the re-granting crash-loop the delegation claims to catch — such a
loop NEVER reaches line 119, so the delegated detector never trips even after
the page_count-only tightening. The tightening is NECESSARY (enables the stop
to fire on the first subsequent zero-admission pass after 60 silent seconds)
but NOT SUFFICIENT for a continuously-granting, zero-page loop.

**Minimal fix (one clause in §4.2's tightening block, mirrored in unit 8):**
"The crawl-side stop fires only on a zero-admission pass: `admission_stall.py:119`
consults `stall_guard.stalled()` solely when `allowed_jobs` is falsy. So the
delegated record is an output-progress *stop* that trips on the first pass
that admits nothing after 60s with no `page_count` growth; it does not trip a
crash-loop that re-grants on every pass while producing no committed pages
(such a loop never reaches the `stalled()` check). The delegation detects 'no
output AND no admission'; it cannot stop a loop that keeps being granted work."

## MAJOR — R8-B2: migration unit 8 conflates kernel with seam: "the kernel
consumes `(CrawlState).page_count`" (§9 unit 8:968-969 × §0:122-124 × §9.6)

The kernel is hard-coded as "independent of the crawler … zero `crawlutron.*`
imports, enforced by an import-contract gate" (§0, §9.6). `CrawlState` is a
crawler type (`recursive.py:268`); `page_count` lives on it. Unit 8's "the
kernel consumes `(CrawlState).page_count` as its caller-side output-progress
source" cannot be read literally — importing `CrawlState` violates the
kernel's own gate. The migration actually changes the SEAM-side
`admission_stall.py:129` record; the kernel's StallGuard keys on `last_advance`
only (§4.4:604 correctly says the kernel "does not instrument the caller's
output"). Also the "seam-parity-relevant" label conflates this crawl-stall
detector seam with §9.6's decision-identity seam-parity.

**Minimal fix (one-line reword):** "This is the seam-parity-relevant change:
the seam's crawl-side stall detector consumes `(CrawlState).page_count` as its
caller-side output-progress source, replacing the raw `allowed_jobs` gate
truthiness — the kernel itself keeps no caller-output visibility (see §4.4)."

## MINOR — R8-B3: two imprecisions on the delegated counter (§4.2:436, unit 8)

**(a) batch-synchronous property unstated** — the review PROVES no false-stall
is reachable: the sole `page_count` mutation is `recursive.py:344`
(`state.page_count += 1`), reached only from `commit_recursive_results`
(`commit_batch`), awaited to completion at `admission_stall.py:128` BEFORE the
record check at line 129 in the same loop pass. A saturated-but-producing box
cannot lag; the false-stall the brief worried about is structurally
impossible — but the doc never states this.
**(b) "actual produced output (a `page_count` increase)" overstates** —
`page_count` also increments on spilled PERMANENT failures
(`_commit_recursive_result` recursive.py:326-345 increments for any
non-transient-requeue result, failures included). For the stall's purpose
arguably correct (a failed fetch is still progress), but "actual produced
pages" is imprecise.

**Minimal fix (one sentence, §4.2 or unit 8):** "The crawl-side record is
batch-synchronous — `commit_batch` (where `state.page_count` increments,
recursive.py:344) runs to completion before the stall check in the same loop
pass, so the record cannot lag the work and a saturated-but-producing box never
false-stalls. Note `page_count` counts committed results including spilled
permanent failures, not only pages returned to the caller."

## Cleared (B3c)

Acceptance rows vs normative: consistent. 13-round Model-A coherence: intact.
Changelog history: kept verbatim and superseded. Inventory: byte-accurate.

## Gate-readiness

Three one-clause corrections (B1 in §4.2 + unit 8, B2 one-line reword,
B3 one sentence) close all findings. The reviewer's explicit position: the
round-7 BLOCKER and every prior resolution are genuinely intact — these are
the v2.7 document's own newest-surface over-promises; a round-9 re-verification
of the corrected clauses clears the document.