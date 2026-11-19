# BRIEF — fix #1131: job listing counts aggregate before LIMIT (#m2_blocker)

Workspace `m2_blocker` (`work/m2_blocker`), directory `/Users/adambarber/Projects/crawlutron/.hardstand/m2_blocker`, branch off main (`13c1d44f`). M2 blocker.

MANDATORY FIRST ACT: load the `agent-onboarding` skill (via the `skill` tool; fallback: read `~/Projects/crawlutron/.opencode/skills/agent-onboarding.md`), then validate the environment (branch `work/m2_blocker`, clean tree apart from untracked briefs, `git rev-parse --show-toplevel`, `git branch --show-current`, `import crawlutron` resolving to the workspace under `PYTHONPATH=<ws>/packages/{extract,safe-url,core,rest,sdk,mcp}/src`, pytest collecting). Any failure: message the parent and HALT.

**Issue #1131: job listing runs a whole-table counts aggregate before LIMIT (24% of list_jobs)** — severity:medium, kind:defect, area:performance, milestone:M2 Correctness & reliability.

Body (from #1129 profile): `_read_job_list` in `packages/core/src/crawlutron/storage/job_store.py:338` calls `self._accumulators.counts_by_job()` — a whole-table aggregate over `crawl_jobs`/`crawl_job_visited`/`crawl_job_results` — for every listing before LIMIT is applied. `counts_by_job()` is still defined at `job_store_accumulators.py:209-220` with at least one caller (not deleted). The #1027 DESC index on `created_at` only covers ORDER BY, not the counts aggregate.

**Fix direction (page-first, then fetch):**
1. `_read_job_list` selects page IDs first: `SELECT job_id FROM crawl_jobs WHERE ... ORDER BY created_at DESC LIMIT ? OFFSET ?` (uses `created_at` index).
2. Then fetches full rows: `SELECT * FROM crawl_jobs WHERE job_id IN (...)` (batched IN, parameterized).
3. `counts_by_job()` is called ONLY if the caller explicitly requests `with_counts=True` (default False).
4. Delete `counts_by_job()` and its helpers once no caller remains.

Your job:
1. Read `packages/core/src/crawlutron/storage/job_store.py` (`_read_job_list`, `list_jobs`, `list_for_caller`) and `job_store_accumulators.py` (`counts_by_job` and all helpers it calls) fully.
2. **Red first:** write a test that proves the whole-table count runs before LIMIT (e.g., time a `list_jobs(limit=10)` on 10k seeded rows and assert the count query path was hit, or mock the count and assert it's called). Record the red output.
3. **Green:** implement page-first ID select + batched fetch; gate `counts_by_job` behind `with_counts`; delete `counts_by_job` and any now-unused helpers.
4. **Refactor:** clean up, no suppressions, comments ≤2 lines, docstrings ≤3 lines.
5. **Walk-past (mutation proof):** revert the page-first logic in a scratch copy (back to calling `counts_by_job` first) and confirm the test reds.
6. Gates: ruff, `ruff format --check`, mypy, basedpyright, jscpd clean.
7. Full randomized battery once solo from workspace cwd.
8. Commit citing #1131. Report hash, battery line, new test wall time.
9. HALT after committing — do not start #991.

Per AGENTS.md: no `# noqa`, `# type: ignore`, or inline suppressions ever. Issue writes are the parent's domain — do not write to issues. Keep replies short (2-3 lines, no code diffs pasted).