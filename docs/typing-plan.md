# Typing programme

**Purpose.** At the original audit, the type checkers were green while the
boundary types were absent: `mypy` reported zero errors across all 106 files in
`app/` and the pyright budget was zero, yet the code had **no structural types
at all** — zero `Protocol`, zero `TypedDict` — and 391 references to `Any`.
Those two facts were compatible only because the configuration asked a narrow
question ("are the types you wrote self-consistent?") and the answer was yes.
This programme replaces that with a question worth asking: *does the type system
know what the code does at the places where untrusted input enters?*

**How to use it.** Work the steps in order. Each step is independently
committable and independently verifiable, and step 1 is not a cleanup — it
removes instructions that were actively suppressing type information. Do not
reorder: step 3's cost depends on which exemptions step 1 removed, and step 4's
scope depends on step 2's types actually landing.

**Baseline commit.** The figures below were re-measured after step 3; the original
`78fcf13` audit remains the historical comparison point. If a number has moved,
the *plan* still holds but the ordering of effort may not.

---

## 1. The measured baseline

Taken with a stdlib `ast` walk over `app/**/*.py` and `mypy`/`importlib.metadata`,
not estimated.

| Measure | Value |
|---|---|
| files in `app/` | 106 |
| functions | 784 |
| arguments | 1,726 — **1,399 annotated (81.1%), 327 bare** |
| return annotations | 751 / 784 (95.8%) |
| `Any` references | **391, across 54 of 106 files** |
| `Protocol` | **10** |
| `TypedDict` | **0** |
| `NamedTuple` / `dataclass` / `cast`+`TypeVar` | 1 / 11 / 15 |
| `mypy.ini` module exemptions | 1 (`yoyo.*` only) |
| `disallow_untyped_defs`, `warn_return_any` | both **off** |
| `mypy` on `app/` | clean, 106 files |
| pyright budget | 0 |

**Where the `Any` lives.** Concentrated on the chokepoints, not spread evenly.
The counts below are current file-level AST reference counts:

| File | `Any` references | Role |
|---|---|---|
| `app/crawl/strategies/browser.py` | 38 | browser transport, screenshots |
| `app/crawl/job_store.py` | 28 | durable state, JSON row serialisation |
| `app/crawl/strategies/http.py` | 25 | the HTTP strategy |
| `app/crawl/http_client_pool.py` | 19 | pooled transports, handle lifecycle |
| `app/api/response_shaping.py` | 21 | the only result-construction path |
| `app/crawl/admission.py` | 18 | request admission |
| `app/crawl/extraction.py` | 20 | document extraction |
| `app/crawl/browser_result.py` | 18 | browser result assembly |
| `app/crawl/request_pipeline.py` | 15 | pipeline assembly |
| `app/models.py` | 13 | the domain taxonomy |

This is the whole argument for the programme. The four worst files are the ones
that touch untrusted bytes and the filesystem, and at the original audit they
were also the files whose external libraries were exempted from checking (step 1).

## 2. The seam is silent exactly where input arrives

At the original audit, every third-party library these chokepoints called
through was excluded from type checking: fourteen exemptions sat in `mypy.ini`.
An `ast`-free check of
the **installed distributions** — does the wheel ship a `py.typed` marker —
separates exemptions that suppress real information from exemptions that paper
over a genuine absence:

| Module | Installed | Ships `py.typed` | Current setting | Verdict |
|---|---|---|---|---|
| `camoufox` | 0.5.6 | **yes** | `ignore_missing_imports` | unjustified |
| `curl_cffi` | 0.16.3 | **yes** | `ignore_missing_imports` | unjustified |
| `trafilatura` | 2.2.0 | **yes** | `ignore_missing_imports` | unjustified |
| `markitdown` | 0.1.7 | **yes** | `ignore_missing_imports` | unjustified |
| `pdfplumber` | 0.11.10 | **yes** | `ignore_missing_imports` | unjustified |
| `playwright` | 1.62.0 | **yes** | `ignore_missing_imports` | unjustified |
| `numpy` | 2.5.3 | **yes** (275 inline `.pyi`) | `follow_imports = skip` | unjustified — see below |
| `brotli` | 1.2.0 | no | `ignore_missing_imports` | stub package exists |
| `regex` | 2026.9.10 | no | `ignore_missing_imports` | stub package exists, and in typeshed |
| `lxml` | 6.1.3 | no | `ignore_missing_imports` | stub package exists |
| `publicsuffixlist` | 1.0.2 | no | `ignore_missing_imports` | **genuinely untyped** |
| `yoyo` | 9.0.0 | no | `ignore_missing_imports` | **genuinely untyped** — see correction below |
| `tests.*`, `scripts.*` | — | — | `ignore_errors` | **dead** — `files = app`, so these never apply |

> **Correction (2026-09-25).** An earlier revision of this table recorded `yoyo`
> as "not installed at all — exempting an import that never happens." **That was
> wrong, and the error was mine.** The measurement queried
> `importlib.metadata.distribution("yoyo")`, which raises
> `PackageNotFoundError`, but the *distribution* is named `yoyo_migrations`
> (its dist-info is `yoyo_migrations-9.0.0.dist-info`) while the *import* name
> is `yoyo`. `importlib.util.find_spec("yoyo")` resolves to
> `site-packages/yoyo/__init__.py` and `yoyo.__version__` is `9.0.0`.
>
> The lesson generalises: **a distribution-name miss is not evidence of
> absence.** Confirm with `find_spec` before writing an exemption off, and when
> reporting a negative, name the lookup that produced it.
>
> The corrected consequence is that `yoyo` is **genuinely untyped** — no
> `py.typed`, no inline `.pyi`, and no stub package on PyPI (`types-yoyo` and
> `yoyo-stubs` are both 404). Its exemption is therefore *justified*, on the same
> footing as `brotli` and `lxml` were before step 2, and restoring it is correct.
> The seven errors that appear when it is removed are all `[import-untyped]` on
> `yoyo` imports under `app/crawl/migrations/` — none is a defect in this
> codebase, and none is evidence that the other nine exemptions are also
> load-bearing.

`numpy`'s exemption is justified in its own comment by "its stub's PEP 695 `type`
statements mypy cannot parse at a 3.11 target". The config now sets
`python_version = 3.12`. The stated reason no longer exists.

**The suppression is free.** Stripping all six `py.typed` exemptions and the
`numpy` skip leaves `mypy` clean at 106 files, zero errors:

```
removed exemptions for: camoufox, curl_cffi, trafilatura, markitdown, pdfplumber, playwright, numpy
remaining module sections: ['tests.*', 'scripts.*', 'brotli', 'regex', 'publicsuffixlist', 'lxml.*', 'yoyo.*']
Success: no issues found in 106 source files
```

So the exemptions were not buying correctness. They were buying silence at
boundaries that were already typed — `curl_cffi` under `http.py` and
`http_client_pool.py`, `playwright` under `browser.py`. That is three of the
four worst `Any` offenders, and we had instructed the checker not to look.

**This is not a free deletion.** `--disallow-any-expr` on
`app/crawl/strategies/fetchers.py` alone returns **47 errors**. The `Any` is
real, and it is not all attributable to the exemptions. Step 1 removes the
cause; it does not remove the symptom.

---

## 3. The programme

### Step 1 — delete the unjustified exemptions

Remove the **nine** sections below: the six `py.typed` libraries, the stale
`numpy` skip, and the two dead sections. `mypy` itself reports the dead ones:
`note: unused section(s): [mypy-tests.*], [mypy-scripts.*]` — someone wrote an
intent that has no effect, and leaving it makes the config look more covered
than it is.

That is the six `py.typed` libraries (`camoufox`, `curl_cffi`, `trafilatura`,
`markitdown`, `pdfplumber`, `playwright`), the stale `numpy` skip, and the two
dead sections. **Do NOT remove the `yoyo` exemption** — see the correction
above; it is justified, because the library is installed and genuinely untyped.

**Verify:** `mypy` still clean at 106 files. If it is not, the exemption was
load-bearing and the finding above is wrong — stop and say so. Do not add the
exemption back to make it green, and do not add any new exemption. A step that
needs a suppression to stay green has failed, and the finding it contradicts is
the important output.

### Step 2 — add stub dependencies rather than hand-writing types

Three of the four genuinely-untyped imports already have maintained stubs.
Checked on PyPI, with upload dates, because a two-year-old stub is worse than a
hand-written Protocol:

| Install | Version | Uploaded | Note |
|---|---|---|---|
| `brotli-stubs` | 1.2.0 | 2026-02-17 | matches installed `brotli` 1.2.0 |
| `types-regex` | 2026.9.10.20260911 | **2026-09-11** | matches installed `regex` 2026.9.10; also in typeshed, so mypy auto-discovers it |
| `types-lxml` | 2026.2.16 | 2026-02-17 | "Complete lxml external type annotation" |

**Do not install `lxml-stubs` 0.5.1** — last uploaded 2024-01-10, two years
stale, and it competes with `types-lxml` for the same import name.

Add these to the dev dependency set so CI installs them.

**Budget this as its own work unit.** Turning on `lxml` and `regex` types was
expected to surface new errors in the extraction path and in `app/safe_regex.py`
— the ReDoS chokepoint. The measured run instead surfaced one
`app/crawl/decompression.py:158` `arg-type` error: the runtime accepts the
bytes-like input that the function already declares, while the stub names only
`bytes`. That was a false positive, fixed with a boundary cast; no lxml or regex
errors appeared. The expectation was useful, but the measured result is the
record.

Steps 1–3 are also the **prerequisite** for the annotation work, not an
alternative to it. Section 4 shows the bare arguments cluster in the chokepoints,
which are precisely the files whose external types are currently suppressed —
so the types that belong in those signatures do not become available until
steps 1 and 2 land.

### Step 3 — hand-written Protocols for what is genuinely untyped

After step 2, two imports remain that no stub package covers:

| Import | Stub package | In typeshed | Surface |
|---|---|---|---|
| `publicsuffixlist` 1.0.2 | none | no | a single function |
| `yoyo` 9.0.0 | none (`types-yoyo`, `yoyo-stubs` both 404) | no | `step`, `get_backend`, `read_migrations`, `DatabaseBackend`, `SQLiteBackend`, `DatabaseURI`, `MigrationList`, `default_migration_table`, used in `app/crawl/migrations/runner.py` plus three one-line `from yoyo import step` migration files |

`publicsuffixlist` is a single-function call; wrap it in a `Protocol` or a thin
typed wrapper so the boundary becomes a contract this codebase owns rather than
a hole it inherits. Give it a real name that says what it is, not
`PublicSuffixListLike` (the implementation uses the private-but-descriptive
`_PublicSuffixDataset`).

`yoyo` is build-time rather than request-path, so the case for typing it is
weaker — but its exemption is exactly the kind of silence this programme
removes, and `runner.py:153-154` already carries a `# type: ignore` written
after someone read yoyo's `Migration.__init__` source directly. Decide
explicitly and record the decision here:

- **Type it** if the migration runner's `get_backend` call can receive a
  caller- or config-derived database URI. That makes it a real boundary.
- **Keep the exemption, with a stated reason and a removal condition**, if it
  cannot. A justified exemption with a reason is debt with a name on it; an
  unexplained one is what step 1 just deleted elsewhere.

Either way, name the surface in one module rather than importing from the
untyped package at each call site, so the decision is visible in one place.

**Decision (2026-09-25): keep the `yoyo` exemption.** In
`app/crawl/migrations/runner.py`, `_backend` accepts a filesystem `db_path`, but
it always constructs the fixed `sqlite:///...` URI before calling `get_backend`;
callers do not provide a database URI or backend selector. The memory backend
name is generated internally. The migration files now import the single
`migration_step` surface from `runner.py`, so the untyped package is named in one
place. Keep `[mypy-yoyo.*]` until yoyo ships a `py.typed` marker or maintained
stubs; the existing `pyright` suppression in `stamp_store` remains because the
runtime `Migration` object has undeclared attributes.

### Step 4 — `disallow_any_expr` per-module, never globally

Enable it on the chokepoints only, starting with `browser.py`, `job_store.py`,
`http.py`, `http_client_pool.py`. A global enable is not the goal — it would
produce a wall of findings in the 45 files that are not chokepoints and would
train the reader to ignore it. Each module gets its own override, and the
count is ratcheted down per module the way the complexity and duplication
budgets already are.

---

## 4. What this programme is explicitly not

**`disallow_untyped_defs` is not the first move — but the reason is the opposite
of the obvious one.** At the original audit, an earlier draft claimed the 326 bare
arguments were "mostly FastAPI route handlers and Alembic migration functions"
where a framework already carries the contract, and therefore not worth
annotating. **That was an inference, and it is false.** Measured attribution of
all 326:

| Bucket | Bare args | Share |
|---|---|---|
| `app/` library and core | **282** | **86.5%** |
| `app/api/` (non-router) | 30 | 9.2% |
| `app/models.py` | 10 | 3.1% |
| Alembic migrations | 4 | 1.2% |
| FastAPI routers | **0** | 0% |

They are not framework boilerplate at all. They cluster in the same files as
the `Any`, and the top offenders are the chokepoints again: `browser.py` 45,
`resource_pressure.py` 26, `scheduler.py` 19, `content_cache.py` 18,
`job_store.py` 17, `http_client_pool.py` 12, `bounded_ttl.py` 11,
`validation.py` 11.

That makes the flag *more* defensible, not less — but it makes it a
**downstream** step. `resource_pressure.py` has 26 bare arguments and its
constructor signature is exactly what `4316d1d` just collapsed two copies of.
Annotating it now means writing down types that do not exist yet, which
produces legible `Any` rather than a contract. Steps 1–3 create the types;
`disallow_untyped_defs` then closes the remaining gaps against something real.
Turning it on first produces a number going down and no safety gained.

**The 5 files where every function has zero argument annotations** are
`app/api/routers/metrics_router.py`, `app/api/ids.py`,
`alembic/versions/0002_crawl_jobs_drop_canonicalized.py`,
`app/api/agent_hints.py`, and `app/api/well_known.py`. Note that
`metrics_router.py` is a router and still contributes zero bare arguments to
the router bucket, which is further evidence the framework-burden story does
not hold. Read these individually rather than as a batch; each has a different
reason and at least one may be legitimate.

## 5. Verification

- `mypy` clean at 106 files after **every** step. A step that needs a new
  exemption to stay green has failed, and the exemption must not be added.
- After step 2, the newly-surfaced errors are triaged as *real defect* or
  *false positive* against live code, one by one. No blanket suppression, no
  `# type: ignore` to make a count go down — a suppression added to pass this
  step is the same defect class this programme exists to remove.
- After step 4, the per-module `Any` counts in section 1 must be strictly
  lower than the baseline, and the reduction must be explained by named
  annotations rather than by widened ignores.
- Full suite green and `ruff check app tests scripts` clean at each commit.

## 6. Status

| Step | Status |
|---|---|
| 1 — delete 9 unjustified/dead sections | complete |
| 2 — add `brotli-stubs`, `types-regex`, `types-lxml` | complete |
| 3 — `Protocol` for `publicsuffixlist`; decide and record `yoyo` | complete |
| 4 — per-module `disallow_any_expr` on 4 chokepoints | not started |

When a step's status changes, the baseline in section 1 is stale and should be
re-measured in the same commit. A plan whose numbers have drifted from the code
is the same failure as a gate that reports success while covering nothing.
