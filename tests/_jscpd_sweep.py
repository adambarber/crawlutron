"""Sweep the jscpd (min-lines, min-tokens) frontier and report what it reveals.

Why this exists. jscpd is a step function on two independent limits: a clone is
reported only when it trips BOTH the line floor and the token floor. So the
counts along the frontier are not a ranking of duplication, they are a
staircase, and the interesting question is not "how many pairs at 4/39" but
*which* limit is binding at each step and *where* the first pair for each file
appears. A file that first appears at 2/20 holds a genuine three-line
repetition that the current floor cannot see at all.

It scans each root separately, for the same reason `_jscpd_gate` does: jscpd
reports paths relative to the scan root, so one whole-tree scan's pair set is
not the union of the per-root pair sets. A sweep run any other way reports a
frontier for a tool configuration that does not exist.

Run: .venv/bin/python tests/_jscpd_sweep.py [--max-lines 4] [--max-tokens 35]
     [--min-lines 2] [--min-tokens 20] [--show N] [--json OUT]
"""

from __future__ import annotations

import argparse
import ast
import concurrent.futures
import json
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _jscpd_gate as gate


def _scan(root: str, min_lines: int, min_tokens: int) -> tuple[str, list[dict]]:
    """One jscpd invocation at one floor, tagged with the root it scanned.

    The invocation itself is the gate's ``jscpd_report`` -- the sweep must not
    hold a second copy of the command line, or the frontier would be a
    staircase over a tool configuration the gate does not use. What the sweep
    adds is the root tag: jscpd reports paths relative to the scan root, so a
    bare name cannot be attributed to a tree.
    """
    report = gate.jscpd_report(root, (min_lines, min_tokens))
    duplicates = (report or {}).get("duplicates") or []
    for duplicate in duplicates:
        for side in ("firstFile", "secondFile"):
            duplicate[side]["tree"] = root
    return root, duplicates


# --- what counts as duplication worth removing ---------------------------------
#
# At 4 lines, Python's own preamble is a clone: three import lines beside three
# import lines is 4 lines and ~25 tokens. Counting those as duplication is how
# a floor gets set by arithmetic instead of by judgement, so every pair is
# classified by what its fragment actually contains. A pair is LOGIC when
# parsing its text yields at least one statement that does something; it is
# PREAMBLE when the fragment is only imports, docstrings, comments, or bare
# constants. Only LOGIC is worth a chokepoint, and only LOGIC should set the
# floor.
_DOCSTRING_OPENERS = ('"""', "'''", 'r"""', "r'''")

# The statements that open a nested scope. This is the vocabulary for "can
# contain a definition", and the metrics gate imports it rather than restating
# it: two lists that happen to agree on these seven are two lists that can stop
# agreeing.
COMPOUND_NODES = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.With,
    ast.AsyncWith,
    ast.Try,
)

# Every compound statement is also one that does something, so the logic
# vocabulary is the nesting vocabulary plus the flat ones. Written as a sum
# rather than as two coincidentally-agreeing tuples, "no statement that opens a
# scope is mere preamble" is a fact here instead of an accident.
LOGIC_NODES = COMPOUND_NODES + (
    ast.Assign,
    ast.AnnAssign,
    ast.AugAssign,
    ast.Return,
    ast.Raise,
    ast.Delete,
    ast.Call,
    ast.Await,
    ast.IfExp,
    ast.BoolOp,
    ast.Compare,
    ast.Assert,
    ast.Yield,
    ast.YieldFrom,
)


def classify(fragment: str) -> str:
    """'logic' or 'preamble' for one jscpd fragment."""
    text = "\n".join(line for line in fragment.splitlines() if not line.strip().startswith("#"))
    if not text.strip():
        return "preamble"
    for candidate in (text, textwrap.dedent(text)):
        try:
            tree = ast.parse(candidate)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom, ast.Pass, ast.Constant)):
                continue
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
                continue  # a docstring
            if isinstance(node, LOGIC_NODES):
                return "logic"
        return "preamble"
    # Unparseable fragment: fall back to a line heuristic, so a classification is
    # never silently a guess.
    stripped = [line for line in text.splitlines() if line.strip()]
    if not stripped or all(
        line.strip().startswith(("import ", "from ", "#") + _DOCSTRING_OPENERS) for line in stripped
    ):
        return "preamble"
    return "logic"


# The gate already measures a duplicate's span; a second measurement is a
# second answer to "how big is this clone", and jscpd found the difference.
_span = gate._footprint


def sweep(min_lines: int, max_lines: int, min_tokens: int, max_tokens: int) -> dict:
    floors = [
        (lines, tokens)
        for lines in range(min_lines, max_lines + 1)
        for tokens in range(min_tokens, max_tokens + 1)
    ]
    roots = list(gate.PROFILE_PATHS["all"])
    results: dict[tuple[int, int], dict[str, list[dict]]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(_scan, root, lines, tokens): (root, lines, tokens)
            for lines, tokens in floors
            for root in roots
        }
        for future in concurrent.futures.as_completed(futures):
            root, duplicates = future.result()
            _, lines, tokens = futures[future]
            results.setdefault((lines, tokens), {})[root] = duplicates
    return results


def _matrix(results: dict) -> str:
    """Pairs per floor, plus duplicated lines -- the staircase in full."""
    roots = sorted(results[sorted(results)[0]])
    header = " ".join(f"{root:>{max(len(root), 6)}}" for root in roots)
    out = [f"  {'floor':>9} | {'pairs':>5} {'dup lines':>9} {header}"]
    for floor in sorted(results):
        by_root = results[floor]
        pairs = sum(len(v) for v in by_root.values())
        lines = sum(_span(d) for v in by_root.values() for d in v)
        cells = " ".join(f"{len(by_root.get(root, [])):>6}" for root in roots)
        out.append(f"  {floor[0]:>4}/{floor[1]:<4} | {pairs:>5} {lines:>9} {cells}")
    return "\n".join(out)


def _binding(results: dict) -> str:
    """Which limit is actually holding the frontier down, per step.

    Raising tokens while lines is fixed removes pairs that were token-poor but
    line-long. Raising lines while tokens is fixed removes pairs that were
    line-short but token-dense. Whichever direction removes pairs is the
    dimension the remaining duplication is being filtered by, and the ratio
    between the two tells you whether the current floor is anywhere near the
    real shape of the code.
    """
    floors = sorted(results)
    out = ["  raising tokens (per line floor) | raising lines (per token floor)"]
    for lines in sorted({lines for lines, _ in floors}):
        same_line = [f for f in floors if f[0] == lines]
        token_deltas = [len(_all(results[f])) for f in same_line]
        lowest = token_deltas[0]
        out.append(
            f"  {lines:>2} lines: {token_deltas} -- "
            f"{(lowest - token_deltas[-1]) / lowest * 100:.0f}% removed by tokens alone"
            if lowest
            else f"  {lines:>2} lines: {token_deltas} -- nothing to remove"
        )
    for tokens in sorted({tokens for _, tokens in floors}):
        same_token = [f for f in floors if f[1] == tokens]
        line_deltas = [len(_all(results[f])) for f in same_token]
        lowest = line_deltas[0]
        out.append(
            f"  {tokens:>2} tokens: {line_deltas} -- "
            f"{(lowest - line_deltas[-1]) / lowest * 100:.0f}% removed by lines alone"
            if lowest
            else f"  {tokens:>2} tokens: {line_deltas} -- nothing to remove"
        )
    return "\n".join(out)


def _all(by_root: dict) -> list[dict]:
    return [duplicate for duplicates in by_root.values() for duplicate in duplicates]


def _first_appearance(results: dict, floors: list) -> list[tuple[tuple[int, int], str, dict]]:
    """The cheapest floor at which each file first holds a pair.

    This is the answer to "where is the real duplication hiding": a file whose
    first appearance is at 2/20 contains a repetition three lines long that the
    shipped 4/39 floor cannot see, and lowering the floor is the only way to
    learn about it.
    """
    seen: dict[str, tuple[tuple[int, int], dict]] = {}
    for floor in floors:
        for duplicate in _all(results[floor]):
            for side in ("firstFile", "secondFile"):
                location = duplicate[side]
                key = f"{location['tree']}/{location['name']}"
                if key not in seen:
                    seen[key] = (floor, duplicate)
    return [
        (floor, key, duplicate) for key, (floor, duplicate) in sorted(seen.items(), key=lambda kv: kv[1][0])
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--min-lines", type=int, default=2)
    parser.add_argument("--max-lines", type=int, default=4)
    parser.add_argument("--min-tokens", type=int, default=20)
    parser.add_argument("--max-tokens", type=int, default=35)
    parser.add_argument("--show", type=int, default=12, help="how many first appearances to print in full")
    parser.add_argument("--json", type=Path, help="write the raw sweep here")
    args = parser.parse_args()

    results = sweep(args.min_lines, args.max_lines, args.min_tokens, args.max_tokens)
    floors = sorted(results)
    roots = sorted(results[floors[0]])

    print(f"jscpd frontier sweep  roots={roots}")
    print(
        f"floors: {args.min_lines}/{args.min_tokens} .. "
        "{args.max_lines}/{args.max_tokens}  ({len(floors)} runs per root)\n"
    )

    print("PAIRS PER FLOOR")
    print(_matrix(results))

    print("\nWHICH LIMIT IS BINDING")
    print(_binding(results))

    print("\nFIRST APPEARANCE PER FILE  (cheapest floor at which the file holds a pair)")
    appearances = _first_appearance(results, floors)
    buckets: dict[tuple[int, int], int] = {}
    for floor, _key, _duplicate in appearances:
        buckets[floor] = buckets.get(floor, 0) + 1
    for floor in floors:
        if floor in buckets:
            print(f"  {floor[0]:>2}/{floor[1]:<2}  {buckets[floor]:>3} files first show a pair here")
    print(f"  total: {len(appearances)} files hold at least one pair at some floor in range")

    print(f"\nDEEPEST HIDING (the {args.show} files whose first pair appears earliest)")
    for floor, key, duplicate in appearances[: args.show]:
        print(f"\n  {key}  --  first at {floor[0]}/{floor[1]}, span {_span(duplicate)} lines")
        for side in ("firstFile", "secondFile"):
            location = duplicate[side]
            print(
                f"    {side[:-4]}: {location['tree']}/{location['name']}:"
                f"{location['start']}-{location['end']}"
            )
        fragment = duplicate.get("fragment", "")
        body = "\n".join("      " + line for line in fragment.splitlines()[:12])
        print(f"    fragment:\n{body}")

    print("\nLOGIC vs PREAMBLE  (a preamble pair is Python's own import/docstring shape)")
    print(f"  {'floor':>6} | {'total':>6} {'logic':>6} {'preamble':>9} {'logic%':>7} {'logic tokens':>13}")
    tipping = []
    for floor in floors:
        if floor[0] != 4:
            continue
        pairs = _all(results[floor])
        kinds = [(duplicate, classify(duplicate.get("fragment", ""))) for duplicate in pairs]
        logic = [d for d, k in kinds if k == "logic"]
        mass = sum(int(d.get("tokens", 0)) for d in logic)
        share = len(logic) / len(pairs) * 100 if pairs else 0.0
        print(
            f"  {floor[0]:>2}/{floor[1]:<3} | {len(pairs):>6} "
            "{len(logic):>6} {len(pairs) - len(logic):>9} {share:>6.0f}% {mass:>13}"
        )
        tipping.append((floor[1], len(logic), share, mass))
    print("\n  the tipping point is the highest token floor at which logic still outnumbers preamble")
    for tokens, logic, share, mass in tipping:
        mark = "  <-- tipping point" if logic and logic * 2 >= tipping[-1][1] else ""
        print(f"    4/{tokens:<3} logic={logic:<4} {share:>3.0f}% of pairs  {mass:>5} logic tokens{mark}")

    print("\nCONCENTRATION  (pairs per production file, over the whole range)")
    per_file: dict[str, int] = {}
    for floor in floors:
        for duplicate in results[floor].get("app", []):
            for side in ("firstFile", "secondFile"):
                name = duplicate[side]["name"]
                per_file[name] = per_file.get(name, 0) + 1
    for name, count in sorted(per_file.items(), key=lambda kv: -kv[1])[:15]:
        if count < 2:
            break
        print(f"  {count:>4} pairs  app/{name}")

    print("\nAT THE CURRENT FLOOR (4/39), and at 2/20, what is left")
    for floor in ((4, 39), (2, 20)):
        if floor not in results:
            continue
        pairs = _all(results[floor])
        print(f"  {floor[0]}/{floor[1]}: {len(pairs)} pairs")
        for duplicate in sorted(pairs, key=_span, reverse=True)[:20]:
            first, second = duplicate["firstFile"], duplicate["secondFile"]
            print(
                f"    {_span(duplicate):>3}L  {first['tree']}/{first['name']}:{first['start']}"
                f"  <->  {second['tree']}/{second['name']}:{second['start']}"
            )

    if args.json:
        payload = {
            "roots": roots,
            "floors": [{"min_lines": lines, "min_tokens": tokens} for lines, tokens in floors],
            "results": {
                f"{lines}/{tokens}": {root: results[(lines, tokens)][root] for root in roots}
                for lines, tokens in floors
            },
        }
        args.json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
