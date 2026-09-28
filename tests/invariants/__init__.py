"""Executable safety invariants, split by what each one is about.

These checks are the round-4 review's central lesson made mechanical: static
rejection of "pathological" shapes is bypassable, and chokepoints only hold if
bypassing them fails CI instead of code review. The AST walks here verify the
UNSAFE patterns are absent from ``app/`` so reintroducing them turns the build
red.

The modules are themed, not alphabetical, so a reviewer asking "how is the
response envelope kept single-shaped?" reads one file rather than grepping
9.5k lines:

``test_url_doors``
    Every URL reaches a sink through a named door.
``test_result_shape``
    Whatever leaves the process is built in exactly one shape.
``test_ownership``
    State that is acquired must be released by the thing that owns it.
``test_bounds``
    Untrusted input is bounded before it is used.
``test_taxonomy``
    The result vocabularies have one home and one spelling.
``test_async_discipline``
    No sync work on the event loop; no defaulted budgets.
``test_duplication``
    Copy-paste is a defect class here, not a style choice.
``test_registry``
    The property registry, and the meta-invariants about this suite.
``test_contracts``
    Declared surfaces: env readers, knobs, DDL, service roots, protocols, tools.
``test_app_tree``
    The traversal every other check depends on.

:mod:`tests.invariants._shared` holds the helpers more than one of them needs.
"""

from __future__ import annotations

__all__: list[str] = []
