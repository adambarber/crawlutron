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
``test_url_frontier``
    Frontier links, host policies and path returns stay validated.
``test_url_egress``
    Joined URLs are egress-filtered; the frontier grows through the funnel.
``test_result_shape``
    Whatever leaves the process is built in exactly one shape.
``test_result_errors``
    Result error fields carry declared shapes, never raw exception text.
``test_wire_mirror``
    Wire models mirror every domain field; advertised keys are declared.
``test_result_status``
    Results derive their status from the taxonomy, never by hand.
``test_result_pipeline``
    Extraction and discovery reach the tree only through the content pipeline.
``test_ownership``
    Locks and caches: no await under a lock, no twin without the helper.
``test_handle_ownership``
    Handles, floors, sweeps and semaphores have one owner.
``test_pool_ownership``
    Pool slots are reaped; cookies enter through the fetch tail.
``test_store_ownership``
    Browser and store state release through their chokepoints.
``test_bounds``
    Untrusted input is bounded before it is used.
``test_taxonomy``
    Each taxonomy artifact has one home and one spelling.
``test_taxonomy_members``
    Passed classifications, strategies, statuses and attempts are enum members.
``test_taxonomy_construction``
    Construction enforces membership; the gate rejects non-members.
``test_taxonomy_literals``
    Literal field checks have one owner; literal dataclasses inherit it.
``test_async_discipline``
    No sync work on the event loop; no defaulted budgets.
``test_duplication``
    Providers and selectors: one contract, one flow.
``test_dedup_construction``
    Scanners, builders, pollers and constructors are never re-inlined.
``test_dedup_filters``
    Filters, pattern caches and comment blocks have one home.
``test_registry``
    The property registry, and the meta-invariants about this suite.
``test_registry_targets``
    The public-target registry: one implementation, observed everywhere.
``test_registry_bool_env``
    The bool-env registry: boolean parsing has one implementation.
``test_registry_robots``
    The robots-policy registry: one shared parser, one policy.
``test_registry_cross_module``
    Registry checks that span modules: detectors resolve imported helpers.
``test_contracts``
    Payloads are built once and strategy runners run guarded.
``test_knob_contracts``
    Every consumed knob is documented, and every documented knob is consumed.
``test_doc_contracts``
    Documents point at paths that exist; manifests set no dead knob.
``test_surface_contracts``
    DDL stays in migrations; every MCP tool declares its validating model.
``test_protocol_contracts``
    Protocol constants are produced; guards and rejections are authored once.
``test_app_tree``
    Every detector walks the same trees and the same nodes, once.
``test_tree_immutability``
    The test suite never mutates the production tree it inspects.
``test_tree_imports``
    Test imports use package paths; private names stay in their package.
``test_tree_vocabulary``
    Vocabulary primitives are referenced; retired buckets are not imported.

:mod:`tests.invariants._shared` holds the helpers more than one of them needs.
"""

from __future__ import annotations

__all__: list[str] = []
