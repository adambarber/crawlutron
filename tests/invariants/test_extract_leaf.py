"""The extract leaf seal: crawlutron_extract imports stdlib only.

No crawlutron_extract module reaches crawlutron or third-party.
"""

from __future__ import annotations

from scripts.module_graph import build_graph

_SEALED_LAYER = "crawlutron_extract."
_INTERNAL = ("crawlutron_extract", "crawlutron_extract.")


def _is_forbidden(target: str) -> bool:
    """Forbidden targets: crawlutron (core wheel) or any third-party module.

    crawlutron_extract internal targets are permitted; anything else that is not
    crawlutron is third-party (the real build_graph never records stdlib edges).
    """
    if target == "crawlutron" or target.startswith("crawlutron."):
        return True
    if target == _INTERNAL[0] or target.startswith(_INTERNAL[1]):
        return False
    return True


def _offender_pairs(modules: dict[str, set[str]]) -> set[str]:
    """Every (extract module -> crawlutron/third-party) reachability pair."""
    offenders: set[str] = set()
    bare = _SEALED_LAYER.rstrip(".")
    sealed = [name for name in modules if name == bare or name.startswith(_SEALED_LAYER)]
    for name in sorted(sealed):
        for reached in sorted(modules.get(name, ())):
            if _is_forbidden(reached):
                offenders.add(f"{name} -> {reached}")
    return offenders


def test_no_extract_module_reaches_crawlutron_or_third_party():
    """The seal: DFS over the runtime import graph; the sealed layer must be
    present first, or a vanished root would green vacuously over an empty set."""
    modules, _importers = build_graph()
    present = any(name == "crawlutron_extract" or name.startswith(_SEALED_LAYER) for name in modules)
    assert present, "crawlutron_extract absent from the walked module graph; seal would green vacuously"
    offenders = _offender_pairs(modules)
    assert not offenders, (
        "crawlutron_extract reaches crawlutron or third-party (leaf violation):\n"
        + "\n".join(sorted(offenders))
    )


def test_extract_leaf_refuses_edge_to_crawlutron():
    """A planted crawlutron_extract -> crawlutron edge is an offender."""
    planted = {
        "crawlutron_extract.parser": {"crawlutron.extraction.content"},
        "crawlutron.extraction.content": set(),
    }
    offenders = _offender_pairs(planted)
    assert "crawlutron_extract.parser -> crawlutron.extraction.content" in offenders


def test_extract_leaf_refuses_third_party_edge():
    """A planted crawlutron_extract -> pdfminer edge is an offender."""
    planted = {
        "crawlutron_extract.pdf_tier": {"pdfminer.high_level"},
        "pdfminer.high_level": set(),
    }
    offenders = _offender_pairs(planted)
    assert "crawlutron_extract.pdf_tier -> pdfminer.high_level" in offenders
