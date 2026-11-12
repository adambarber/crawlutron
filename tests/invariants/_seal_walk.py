"""The one module-graph reach walk both layering seals run.

``_reachable`` (DFS over ``scripts.module_graph``'s edge map) and the sealed-vs- forbidden offender sweep
live here so sibling seals cannot drift into divergent walks; each seal declares only its layers.
"""

from __future__ import annotations

__all__ = ["reachable", "offender_pairs"]


def reachable(modules: dict[str, set[str]], start: str) -> set[str]:
    """Every module transitively reachable from ``start`` (inclusive of start)."""
    seen: set[str] = set()
    queue = [start]
    while queue:
        current = queue.pop()
        if current in seen:
            continue
        seen.add(current)
        queue.extend(modules.get(current, ()))
    return seen


def offender_pairs(
    modules: dict[str, set[str]], sealed_prefix: str, forbidden_prefixes: tuple[str, ...]
) -> set[str]:
    """Every (sealed module -> forbidden module) reachability pair, DFS closure."""
    offenders: set[str] = set()
    sealed = [name for name in modules if name.startswith(sealed_prefix)]
    for name in sorted(sealed):
        offenders.update(
            f"{name} -> {reached}"
            for reached in reachable(modules, name)
            if reached.startswith(forbidden_prefixes)
        )
    return offenders
