"""A page's durable identity lives on the URL object, never spelled at call sites.

A job stores its URLs in collections that must agree with each other --
``visited``, ``in_flight`` and ``frontier`` -- and every one of those is a set
or dict keyed by identity. Since the NormalizedUrl convergence, identity is a
property of the value object: an admitted value IS its own durable key, and
``.raw`` is by definition the stored form (see the class docstring).

The retired architecture spelled identity out as a fallback at call sites::

    canonical_page_identity(url, host_policy=hp) or url

so a refused URL still occupied a key made of its raw text. That category of
keying is gone by design: a URL that admission refuses is skipped and
reported, not keyed. The hazard this gate now watches for is the same drift
rebuilt on the current seam -- an ``or``-fallback that resurrects raw text a
refusing admission call just declined::

    admit_or_none(url, policy=hp) or url
    base.resolve(ref) or ref

Every one of those reintroduces "refused but keyed anyway" one call site at a
time, which is exactly how the eleven scattered copies arrived the first time.
"""

import ast
import pathlib

from tests.chokepoint_walks import (
    offender_at,
    or_nodes,
    resolved_call_name,
)
from tests.rules import Registry, Rule

#: The calls that refuse -- return None -- instead of keying a bad URL.
#: ``admit_or_none`` is the module-level door; ``resolve`` is the value's own
#: admission of a reference against its embedded policy. Both must stay
#: fallback-free: the None IS the verdict, and ``or <raw>`` overrules it.
_REFUSING_NAMES = frozenset({"admit_or_none", "resolve"})


def _inline_identity_fallbacks(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag a call site that spells the raw-text fallback out as ``or <raw>``.

    Keyed on the *fallback* -- a refusing call being ``or``-ed into a value
    that survives it -- rather than on any one module's spelling, so a new
    call site written in a new way is caught too.
    """
    offenders: list[str] = []
    for node in or_nodes(tree):
        if _names_refusing_call(tree, node):
            offenders.append(offender_at(path, node, "inline-identity-fallback"))
    return offenders


def _names_refusing_call(tree: ast.AST, node: ast.AST) -> bool:
    """True when a refusing admission call is a DIRECT operand of the ``or``.

    Matched on the *leaf* of the resolved name. ``resolved_call_name`` answers
    with the import-resolved dotted path, so a consumer calling
    ``admit_or_none`` reads as ``app.models.normalized_url.admit_or_none``
    while the defining module reads as the bare name. Comparing the whole
    string would fire in exactly one file -- the one that defines the
    function -- and report a clean sweep of the call sites that are the
    actual problem.

    Only a direct operand is the fallback: ``<refusing call> or <raw>``. A
    call nested deeper inside an operand -- ``<other> or <refusing call> is
    not None`` -- is the correct refusal question, not an overruling of it.
    """
    return any(
        isinstance(value, ast.Call)
        and resolved_call_name(tree, value).rsplit(".", 1)[-1] in _REFUSING_NAMES
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


def test_page_identity_fallback_detector_bites():
    """The sweep must fire on a hand-rolled fallback and stay off a lookalike.

    Without the clean case this either flags some unrelated ``or`` -- a detector
    that cries wolf is a detector that gets deleted -- and without the firing
    case it is a gate that proves nothing. Both are declared on the rule, so
    neither can be dropped without the declaration failing to build.
    """
    RULES.check_all_bite()


def test_admitted_raw_is_the_durable_identity():
    """The owner's contract: an admitted value's stored form is its ``.raw``.

    Stated as behavior rather than as the AST shape the gate above enforces,
    because the two are independent. A future refactor that keeps every call
    site free of inline fallbacks but changes what identity means would pass
    the gate and silently re-key every stored collection.
    """
    from app.models.normalized_url import NormalizedUrl
    from tests._policy_factories import UNRESTRICTED_HOST_POLICY

    # The canonical public form, with the noise the normalizer strips
    # (default port, empty path, fragment) removed, IS the stored identity.
    admitted = NormalizedUrl(
        "https://Example.COM:443/a/b/?b=2&a=1#frag", policy=UNRESTRICTED_HOST_POLICY
    )
    assert admitted.raw == "https://example.com/a/b?a=1&b=2"

    # Identity is stable across constructions of the same URL, which is what
    # set/dict keying by the value depends on.
    again = NormalizedUrl("https://example.com/a/b?a=1&b=2", policy=UNRESTRICTED_HOST_POLICY)
    assert admitted == again
    assert hash(admitted) == hash(again)
