#1045 — Migrate all remaining exempt storage writes onto txn_write/in_txn and seal the write-door gate

## Objective
Migrate the 9 remaining exempt storage modules onto `txn_write`/`in_txn` so every storage write funnels through the write door (`safe_db.py`). Remove each module from `_EXEMPT_MODULES` in `tests/invariants/test_write_door.py` as it is migrated (ratchet down). Once all are migrated, delete `_EXEMPT_MODULES` entirely, drop the `skip` set, and seal the gate with an unconditional assertion walking every storage module.

## Exempt modules (from _EXEMPT_MODULES)
1. `admin_audit_store.py` — `record_event`: INSERT + `_roll()` (2 DELETEs) + `commit()`
2. `caller_key_store.py` — `issue_key` (INSERT + commit), `revoke_key` (DELETE + commit), `rotate_key` (UPDATE + commit), `_commit_and_audit` (bare commit)
3. `coordination_cookies.py` — `merge`, `clear`, `touch`, `put` all use `BEGIN IMMEDIATE`/`COMMIT`/`ROLLBACK` manually; `_upsert_slot` bare INSERT
4. `coordination_sync_slots.py` — `reserve`, `release`, `stale` use `BEGIN IMMEDIATE`/`COMMIT`/`ROLLBACK` manually
5. `job_store.py` — `_persist` (execute + accumulators.append + commit), `_tombstone_row` (execute + rollback/commit)
6. `job_store_accumulators.py` — `delete`, `_frontier_resync`, `_frontier_write_tail`, `_reconcile_frontier`, `_append_results` ×2, `_append_visited` ×2 — bare writes, ride JobStore's implicit txn
7. `job_store_lease.py` — `_execute` (bare execute + commit); all lease ops (`claim_running`, `renew_job_claim`, `release_job_claim`, `clear_job_claim`, `retire_orphan_running`, `set_status`) go through `_execute`
8. `result_store.py` — `CrawlResultStore.put` (_prune DELETE + INSERT + commit ×2), `CrawlResultStore.delete` (DELETE + commit), `CrawlSpillStore.put` (INSERT ×2 + commit), `release_spill` (DELETE + commit), `gc_stale_spills` (CREATE TEMP TABLE + DELETE + INSERT + DELETE + commit)
9. `watch_store.py` — `create` (INSERT + commit), `delete`/`delete_for_caller` (DELETE + commit), `claim_due` (UPDATE + commit), `renew_claim` (UPDATE + commit), `record_check` (UPDATE + commit), `deactivate` (UPDATE + commit)

## Migration pattern (established by #1035/#1043/#1044)
- `txn_write(self._conn, owner="module.method")` — wraps writes in `BEGIN IMMEDIATE`/`COMMIT`/`ROLLBACK on BaseException`
- `in_txn(self._conn)` — for methods that participate in an existing transaction (asserts `conn.in_transaction`)
- `atwin` twins already exist on all stores; keep them
- Replace bare `BEGIN IMMEDIATE`/`COMMIT`/`ROLLBACK` with `txn_write`
- Replace bare `execute`/`executemany` with INSERT/UPDATE/DELETE inside `txn_write`/`in_txn`
- Remove bare `commit()` calls (txn_write handles commit)

## Per-module migration notes
- **admin_audit_store**: `record_event` → wrap INSERT + `_roll()` in `txn_write`; remove bare `commit()`. `_roll`'s DELETEs are inside `record_event`'s `txn_write` block → pass `_inside_door`.
- **caller_key_store**: `issue_key`, `revoke_key`, `rotate_key` → wrap in `txn_write`; remove bare `commit()`. `_commit_and_audit` → inline or fold into `txn_write` block.
- **coordination_cookies**: `merge`, `clear`, `touch`, `put` → replace `BEGIN IMMEDIATE`/`COMMIT`/`ROLLBACK` with `txn_write`. `_upsert_slot` is called inside `txn_write` blocks → passes `_inside_door`.
- **coordination_sync_slots**: `reserve` → replace manual txn with `txn_write` (keep lock outside). `release`, `stale` → wrap in `txn_write`, remove bare `commit()`.
- **job_store**: `_persist` → wrap `execute` + `append` in `txn_write`; remove bare `commit()`. `_tombstone_row` → wrap in `txn_write`; remove bare `commit()`/`rollback()`. JobAccumulators methods called from `_persist` → use `in_txn` to assert inside transaction.
- **job_store_accumulators**: All methods (`delete`, `_frontier_resync`, `_frontier_write_tail`, `_reconcile_frontier`, `_append_results` ×2, `_append_visited` ×2) → wrap writes in `in_txn` (they ride JobStore's transaction from `_persist`).
- **job_store_lease**: `_execute` → wrap `execute` in `txn_write`, remove bare `commit()`. All lease ops go through `_execute`.
- **result_store**: `CrawlResultStore.put` → wrap `_prune` + INSERT + DELETE in `txn_write`; remove bare `commit()`s. `CrawlResultStore.delete` → wrap in `txn_write`. `CrawlSpillStore.put` → wrap INSERTs in `txn_write`. `release_spill`, `gc_stale_spills` → wrap in `txn_write`.
- **watch_store**: `create`, `delete`, `delete_for_caller`, `claim_due`, `renew_claim`, `record_check`, `deactivate` → wrap in `txn_write`; remove bare `commit()` calls.

## Ratchet down
Remove each module from `_EXEMPT_MODULES` as it is migrated (the `scope` and `skip` derive from `_EXEMPT_MODULES`). Commit each module's migration separately with TDD red→green→refactor.

## Seal the gate (final step)
When all 9 modules are migrated and removed from `_EXEMPT_MODULES`:
- Delete `_EXEMPT_MODULES` entirely (the frozenset and its docstring)
- Remove `skip=frozenset(_EXEMPT_MODULES)` from the `Rule`
- Change `scope` to walk all storage modules (no exclusion)
- Leave an unconditional assertion: `test_storage_writes_funnel_through_the_write_door` walks everything, no exemption

## Verification requirements (each module + final)
- TDD: red first (gate fails naming bare writes), then green, then refactor
- Mutation proof: restore bare writes → gate REDs naming file:line
- Full randomized battery must pass (~55s)
- ruff check, ruff format, mypy, basedpyright, jscpd, vulture all clean
- No magic comments, no issue numbers in file names, comment runs ≤2 lines, docstrings ≤3 lines, files ≤500 lines
- Every commit message cites #1045

## Environment
- Workspace: `.hardstand/issue_1045` on branch `work/issue_1045`
- Venv: `.hardstand/issue_1045/.venv` → `../../.venv`
- Run tests with `PYTHONPATH` prepended with workspace `packages/*/src` (venv editable .pth points at main checkout — see AGENTS.md #909)
- Use `hardstand run issue_1045 <action>` for declared actions (pytest, mypy, ruff, etc.)
