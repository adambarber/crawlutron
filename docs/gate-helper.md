# The gate helper entry point (`scripts/gate`)

`scripts/gate` is the one gate entry point every hook in
`.pre-commit-config.yaml` runs through. This file is the reference for the
design decisions that used to live in the helper's banner comment block.

## Why `scripts/gate` exists

All five hooks used to open with a verbatim copy of the same two-line
preamble:

```bash
bash -c 'cd "$(git rev-parse --show-toplevel)/docker/browser-crawler" 2>/dev/null
|| cd "$(git rev-parse --show-toplevel)"; exec "$@"' gate
```

Five copies that had to be edited together, and nothing compared them, so the
gate unified one trigger (`*service_trigger`) and left five preambles. The
trigger could become a YAML anchor because its value is a plain string; the
preamble could not, because YAML has no string interpolation. It became a
committed file instead -- whose only decision, which root is the service, now
lives in one place.

## What stays in the entry

The entry still names two candidate paths for this file. That is irreducible:
the helper sits at `scripts/gate` here and at
`docker/browser-crawler/scripts/gate` when the tree is vendored into a
monorepo, and pre-commit always runs hooks with the repository root as the
working directory. One file cannot be named by one relative path in both
layouts, so the lookup cannot move into the helper. What moved into the helper
is everything that can move.

The dual-layout resolution is asserted by execution, not by substring-matching
the config: `tests/test_hook_contract.py` builds a synthetic repository in each
layout and requires the real entry string to run and resolve the right service
root.

## The missing-venv guard

The gate commands name the service's own pinned interpreter
(`.venv/bin/python -m ruff ...`) and are handed straight through. The one thing
worth saying out loud is what a missing venv looks like: `exec` on its own
reports "No such file or directory" and blames the gate command, which sends
the reader looking in the wrong file. The helper therefore checks for the
interpreter first and names the real toolchain source (pyproject.toml's dev
extra).

A gate that runs nothing confirms nothing: `exec "$@"` with an empty list is a
bash no-op, so a zero-argument call is a dropped hook command, not a valid run.
The helper fails such a call loudly.