"""A page's durable identity is computed by one function, not spelled per call site.

A job stores its URLs in three collections that must agree with each other --
``visited``, ``in_flight`` and ``frontier`` -- and every one of those is a set
or dict keyed by identity. The identity of a URL is its canonical public form
under the job's host policy, with the raw URL as the fallback when the URL has
no canonical form (it failed the SSRF policy, or it is not a URL at all).

That fallback was written out at eleven call sites across three modules, in
three spellings::

    canonical_page_identity(url, host_policy=hp) or url
    str(canonical_page_identity(value, host_policy=hp) or value)

and eleven hand-written copies of one rule is eleven places for it to be
wrong. Two of them already had drifted from the store's own version in a way
that cost something measurable: ``_canonical_or_raw`` is memoized because
re-deriving identity for every stored URL on every read of a 20k-entry job
took ~0.23s per load, and every one of the eleven scattered call sites
recomputed what the store had already cached.

The owner is ``durable_page_identity`` and the fallback lives inside it.
``canonical_page_identity`` stays for the callers that genuinely need to
distinguish "no identity" from "identity equal to the raw URL" -- that
distinction is the whole reason admission refuses a URL rather than keying it.
This gate keeps the fallback from being written out again.
"""

import ast
import pathlib

from tests.chokepoint_walks import (
    REPO_ROOT,
    app_offenders,
    assert_detector,
    nodes_of,
    offender_at,
    or_nodes,
    resolved_call_name,
)

_OWNER = "durable_page_identity"


def _inline_identity_fallbacks(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag a call site that spells the identity fallback out as ``or <raw>``.

    Keyed on the *fallback* -- a call that can return ``None`` being
    ``or``-ed into a value that survives it -- rather than on any one module's
    spelling, so a twelfth call site written in a fourth way is caught too.

    The owner's own body is where the rule is allowed to exist, and so is the
    one function that legitimately wants the ``None``/value distinction; both
    are excluded by name so this detects copies rather than the definition.
    """
    offenders: list[str] = []
    for node in or_nodes(tree):
        if _names_canonical_call(tree, node):
            offenders.append(offender_at(path, node, "inline-identity-fallback"))
    return offenders


def _names_canonical_call(tree: ast.AST, node: ast.AST) -> bool:
    """True when ``node`` contains a call to the None-returning identity function.

    Matched on the *leaf* of the resolved name. ``resolved_call_name`` answers
    with the import-resolved dotted path, so a consumer calling
    ``canonical_page_identity`` reads as
    ``app.crawl.admission.canonical_page_identity`` while the defining module
    reads as the bare name. Comparing the whole string would fire in exactly
    one file -- the one that defines the function -- and report a clean sweep
    of the ten call sites that are the actual problem.
    """
    return any(
        resolved_call_name(tree, child).rsplit(".", 1)[-1] in {"canonical_page_identity", _OWNER}
        for child in nodes_of(node)
        if isinstance(child, ast.Call)
    )


def test_page_identity_fallback_is_not_written_out_at_call_sites():
    offenders = app_offenders(_inline_identity_fallbacks)
    assert not offenders, (
        "identity is computed as `canonical_page_identity(...) or <raw>` at a call "
        f"site instead of through {_OWNER}():\n  " + "\n  ".join(offenders)
    )


def test_page_identity_fallback_detector_bites():
    """The sweep must fire on a hand-rolled fallback and stay off a lookalike.

    Without the clean case this either flags some unrelated ``or`` -- a detector
    that cries wolf is a detector that gets deleted -- and without the firing
    case it is a gate that proves nothing.
    """
    assert_detector(
        _inline_identity_fallbacks,
        path=REPO_ROOT / "app" / "_synthetic.py",
        fires={
            "def f(u, hp):\n    return canonical_page_identity(u, host_policy=hp) or u\n": (
                "inline-identity-fallback"
            )
        },
        clean=(
            # An `or` over something that cannot be None is a different rule.
            "def f(a, b):\n    return a or b\n",
            # The owner itself is where the fallback belongs.
            "def durable_page_identity(u, hp):\n    return _raw(u) or u\n",
        ),
    )


def test_durable_page_identity_returns_the_stored_form():
    """The owner's contract: canonical form when there is one, raw otherwise.

    Stated as behavior rather than as the AST shape the gate above enforces,
    because the two are independent. A future refactor that keeps every call
    site reading through the owner but changes what the owner returns would
    pass the gate and silently re-key every stored collection.
    """
    from app.crawl.job_store import durable_page_identity
    from app.validation import HostPolicy

    policy = HostPolicy.unrestricted()

    # A canonical public URL keeps its canonical form, with the noise the
    # normalizer strips (default port, empty path, fragment) removed.
    assert (
        durable_page_identity("https://Example.COM:443/a/b/?b=2&a=1#frag", policy)
        == "https://example.com/a/b?a=1&b=2"
    )

    # A URL with no canonical form falls back to exactly what it was, so a
    # refused target still occupies a stable key instead of vanishing.
    assert durable_page_identity("", policy) == ""
    assert durable_page_identity("not a url", policy) == "not a url"
