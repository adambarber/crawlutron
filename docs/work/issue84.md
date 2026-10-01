## What

`tests/conftest.py` -- the fixture that runs before every single test -- reaches
into five `app/` modules to reset process-wide state. Thirteen private names:

| caller alias | module | names |
| --- | --- | --- |
| `pinned_mod` | `pinned_transport` | `_DNS_CACHE`, `_check_ssrf_protection`, `_resolve_and_pin_host` |
| `pool_mod` | `http_client_pool` | `_PENDING_CLIENT_CLOSE`, `_PENDING_CLIENT_CLOSE_LOCK`, `_DEFAULT_CFFI_SESSION`, `_DEFAULT_HTTP_CLIENT` |
| `cookie_mod` | `cookie_jar` | `_GLOBAL_COOKIE_JAR`, `_COOKIE_NAMESPACE` |
| `_engine_mod` | `engine` | `_resource_gate`, `_SHARED_HTTP_CACHE` |
| `fh` | `fetch_helpers` | `_SITEMAP_CACHE` |

Separately, `_DNS_CACHE.clear()` alone is called at **seven** sites across four
test files, each hand-rolling the same reset.

## Why it matters

The module boundary is currently the *state object* rather than the *owner*.
Every holder of process-wide state is silently obliged to publish a reset, and
the only way for a caller to fulfil that obligation is to reach in.

The codebase already has the right shape once: `cgroup_sense` exposes
`_invalidate_cgroup_cache()` and conftest calls it. Applied inconsistently, which
means the good shape was never the rule -- it was a one-off.

The cost is that a new process-wide cache can be added with no obligation to
expose anything, and the first person to need isolation will add a ninth
`.clear()` site instead of asking the owner for a door.

## What "done" means

1. Each of the five modules exposes one public reset for the state it owns.
   `_DNS_CACHE` and friends stay private: the reset is the door, not the alias.
2. All seven `.clear()` sites and the conftest reach-in go through those resets.
3. A gate: `tests/conftest.py` may not reach a private `app/` name at all.

The gate is the part that stops it coming back. Without it this is a cleanup;
with it, the pattern is enforced.

## Note on scope

This sits *under* #70-#82 rather than beside them. Those issues say "stop reaching
past this module's surface"; this one says the surface is missing a door, and
supplies it for the five modules the harness depends on most.