"""A page's durable identity lives on the URL object, never spelled at call sites.

An admitted URL IS its own durable key; the gate watches for any ``or <raw>``
fallback that resurrects refused raw text at a call site.
"""

import ast
import pathlib

from tests.chokepoint_walks import (
    offender_at,
    or_nodes,
    resolved_call_name,
)
from tests.rules import Registry, Rule

#: ``admit_or_none`` is the module-level door; ``resolve`` is the value's own admission of a reference against
#: its embedded policy.
_REFUSING_NAMES = frozenset({"admit_or_none", "resolve"})


def _inline_identity_fallbacks(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag a call site that spells the raw-text fallback out as ``or <raw>``.

    Keyed on the fallback (a refusing call ``or``-ed into a surviving value)
    rather than any module's spelling, so a new call site is caught.
    """
    offenders: list[str] = []
    for node in or_nodes(tree):
        if _names_refusing_call(tree, node):
            offenders.append(offender_at(path, node, "inline-identity-fallback"))
    return offenders


def _names_refusing_call(tree: ast.AST, node: ast.AST) -> bool:
    """True when a refusing admission call is a DIRECT operand of the ``or``.

    Matched on the leaf so callers read as full paths without firing in the
    defining module; a nested call is the correct refusal, not an overruling.
    """
    return any(
        isinstance(value, ast.Call) and resolved_call_name(tree, value).rsplit(".", 1)[-1] in _REFUSING_NAMES
        for value in getattr(node, "values", [])
    )


PAGE_IDENTITY_FALLBACK = Rule(
    name="page-identity-fallback",
    find=_inline_identity_fallbacks,
    summary=(
        "identity is spelled as `<admission call>(...) or <raw>` at a call "
        "site, resurrecting the raw text that admission just refused"
    ),
    bite=lambda: (
        "def f(u, hp):\n    return admit_or_none(u, policy=hp) or u\n",
        # The same rule against the value's own resolution door.
        "def f(base, ref):\n    return base.resolve(ref) or ref\n",
    ),
    clean=lambda: (
        # An `or` over something that cannot be None is a different rule.
        "def f(a, b):\n    return a or b\n",
        # `is not None` is the correct question: refusal stays refusal.
        "def f(u, hp):\n    return admit_or_none(u, policy=hp) is not None\n",
    ),
)

RULES = Registry((PAGE_IDENTITY_FALLBACK,))


def test_page_identity_fallback_is_not_written_out_at_call_sites():
    RULES.check_all_clean()


def test_admitted_raw_is_the_durable_identity():
    """The owner's contract: an admitted value's stored form is its ``.raw``.

    Stated as behavior, not the AST shape, because the two are independent and
    a refactor that changed identity could pass the gate and re-key everything.
    """
    from app.models.normalized_url import NormalizedUrl
    from tests._policy_factories import UNRESTRICTED_HOST_POLICY

    # The canonical public form, with the noise the normalizer strips (default port,
    # fragment) removed, IS the stored identity. Path text survives verbatim (#520).
    admitted = NormalizedUrl("https://Example.COM:443/a/b/?b=2&a=1#frag", policy=UNRESTRICTED_HOST_POLICY)
    assert admitted.raw == "https://example.com/a/b/?a=1&b=2"

    # Identity is stable across constructions of the same URL, which is what set/dict keying by the value
    # depends on.
    again = NormalizedUrl("https://example.com/a/b/?a=1&b=2", policy=UNRESTRICTED_HOST_POLICY)
    assert admitted == again
    assert hash(admitted) == hash(again)
