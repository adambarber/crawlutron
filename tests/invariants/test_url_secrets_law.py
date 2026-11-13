"""The URL-secrets law, as one zero-exemption AST gate over the whole app tree: every rule here sweeps
``crawlutron/`` with no allowlist, no budget and no per-file exclusions.
"""

from __future__ import annotations

import ast
import pathlib

from tests.chokepoint_walks import (
    APP_ROOT,
    app_trees,
    enclosing_function,
    functions,
    nodes_of,
    offender_at,
    parent_map,
    relative_posix,
    resolved_expr_name,
)
from tests.rules import Bites, Registry, Rule, Silent

#: The rest package's api dir, for the path-keyed bite proofs that must prove
#: a silenced site in the moved surface (relative_posix renders it rest-form).
_REST_API = (
    pathlib.Path(__file__).resolve().parents[2] / "packages" / "rest" / "src" / "crawlutron_rest" / "api"
)

# The two admission edges (watch + async crawl) and the delivery POST site:
# the only modules a raw ``webhook_url: str`` may appear in a signature of.
_ADMISSION_EDGES = frozenset(
    {
        "packages/rest/src/crawlutron_rest/api/watch_router.py",
        "packages/rest/src/crawlutron_rest/api/crawl_endpoint.py",
    }
)

#: The delivery POST site and its validation helpers: raw str is the wire
#: form here, inside the one delivery module.
_DELIVERY_SITE = "packages/core/src/crawlutron/service/webhooks.py"

#: The persistence/identity owner: the durable text is stored and read back
#: here, and every consumer re-admits it.
_PERSISTENCE_SITE = "packages/core/src/crawlutron/storage/watch_store.py"

#: The model that owns the raw durable text; its projections are all redacted.
_RAW_OWNER = "packages/core/src/crawlutron/models/watch_state.py"

_SANCTIONED_RAW_STR_MODULES = _ADMISSION_EDGES | {_DELIVERY_SITE, _PERSISTENCE_SITE, _RAW_OWNER}


def _annotation_text(annotation: ast.expr | None) -> str:
    """An annotation as text, ``""`` when absent."""
    return ast.unparse(annotation) if annotation is not None else ""


def _is_target_or_normalized(annotation: ast.expr | None) -> bool:
    """True for a parameter typed ``WebhookTarget`` or ``NormalizedUrl`` (either side of a union)."""
    text = _annotation_text(annotation)
    return "WebhookTarget" in text or "NormalizedUrl" in text


def _raw_webhook_url_param_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag any production parameter named ``webhook_url`` that holds raw ``str``. Outside the two admission
    edges, the delivery POST site and the persistence owner, the sanctioned shape is WebhookTarget /
    NormalizedUrl / None -- a ``str`` parameter is a sink the type system cannot guard.
    """
    if relative_posix(path) in _SANCTIONED_RAW_STR_MODULES:
        return []
    offenders: list[str] = []
    for func in functions(tree):
        for arg in (*func.args.args, *func.args.kwonlyargs, *func.args.posonlyargs):
            if arg.arg != "webhook_url" or _is_target_or_normalized(arg.annotation):
                continue
            text = _annotation_text(arg.annotation) or "<untyped>"
            offenders.append(
                offender_at(
                    path,
                    arg,
                    f"{func.name}() takes webhook_url: {text} -- "
                    "raw webhook URLs may enter only as WebhookTarget",
                )
            )
    return offenders


# The union ban (clause 4) is the same signature walk widened to every
# parameter of every module: a union is a str door wearing the type's name.
def _webhook_target_str_union_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag a production signature spelling ``WebhookTarget | str`` (either order)."""
    offenders: list[str] = []
    for func in functions(tree):
        args = (*func.args.args, *func.args.kwonlyargs, *func.args.posonlyargs)
        if func.args.vararg is not None:
            args = (*args, func.args.vararg)
        if func.args.kwarg is not None:
            args = (*args, func.args.kwarg)
        for arg in args:
            parts = [part.strip() for part in _annotation_text(arg.annotation).split("|")]
            if "WebhookTarget" in parts and "str" in parts:
                offenders.append(
                    offender_at(
                        path,
                        arg,
                        f"{func.name}() declares a WebhookTarget | str union -- "
                        "str converts to WebhookTarget only at the admission edge",
                    )
                )
    return offenders


# ``delivery_url()`` may be read only at the sanctioned webhook_url sites
# plus the pre-delivery validation edge (watch_router).
_RAW_ACCESSOR_READER_SITES = _SANCTIONED_RAW_STR_MODULES | {
    "packages/rest/src/crawlutron_rest/api/watch_router.py"
}


def _raw_delivery_url_read_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag a read of the raw webhook URL accessor outside the sanctioned sites."""
    if relative_posix(path) in _RAW_ACCESSOR_READER_SITES:
        return []
    offenders: list[str] = []
    parents = parent_map(tree)
    for node in nodes_of(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr == "delivery_url":
            offenders.append(
                offender_at(path, node, "delivery_url() read outside the delivery/persistence doors")
            )
        elif isinstance(node, ast.Attribute) and node.attr == "raw":
            if resolved_expr_name(tree, node.value) in {"WebhookTarget", "target"}:
                owner = enclosing_function(node, parents)
                name = owner.name if isinstance(owner, ast.FunctionDef | ast.AsyncFunctionDef) else ""
                if name != "delivery_url":
                    offenders.append(
                        offender_at(path, node, "WebhookTarget raw read outside the delivery door")
                    )
    return offenders


RAW_WEBHOOK_URL_MOVES_THROUGH_THE_ADMITTED_TARGET = Rule(
    name="raw-webhook-url-moves-through-the-admitted-target",
    find=_raw_webhook_url_param_offenders,
    summary=(
        "Every webhook_url consumer outside the admission/delivery/persistence doors "
        "must hold WebhookTarget or NormalizedUrl -- a raw str parameter is a sink the "
        "type system cannot stop receiving a credential-carrying URL"
    ),
    bite=lambda: (
        Bites(
            "def _deliver_webhook(webhook_url: str | None, payload: dict) -> None:\n    return None\n",
            "raw webhook URLs may enter only as WebhookTarget",
        ),
        Bites(
            "def _deliver_webhook(webhook_url, payload: dict) -> None:\n    return None\n",
            "raw webhook URLs may enter only as WebhookTarget",
        ),
        # The path-keyed half: the same raw-str parameter IS flagged at the
        # pipeline, which no sanctioned door covers.
        Bites(
            "def crawl(webhook_url: str | None) -> None:\n    return None\n",
            "raw webhook URLs may enter only as WebhookTarget",
            at=APP_ROOT / "orchestration" / "request_pipeline.py",
        ),
    ),
    clean=lambda: (
        # A WebhookTarget | None parameter is the sanctioned shape.
        Silent(
            "async def _deliver_webhook(webhook_url: WebhookTarget | None, payload: dict) -> None:\n"
            "    return None\n",
            at=APP_ROOT / "service" / "async_crawl_runner.py",
        ),
        # Inside a sanctioned door the raw str parameter is the wire form.
        Silent(
            "def ensure_https_webhook_url(webhook_url: str) -> None:\n    return None\n",
            at=APP_ROOT / "service" / "webhooks.py",
        ),
        Silent(
            "def store(webhook_url: str | None) -> None:\n    return None\n",
            at=APP_ROOT / "storage" / "watch_store.py",
        ),
        Silent(
            "def admit(payload, *, webhook_url: str | None) -> None:\n    return None\n",
            at=_REST_API / "watch_router.py",
        ),
    ),
)

WEBHOOK_TARGET_IS_THE_BOUNDARY_TYPE = Rule(
    name="webhook-target-is-the-boundary-type",
    find=_webhook_target_str_union_offenders,
    summary=(
        "Zero WebhookTarget | str unions in production signatures: str->WebhookTarget "
        "conversion happens once at the outermost admission edge, never per caller"
    ),
    bite=lambda: Bites(
        "def post(target: WebhookTarget | str, payload: dict) -> None:\n    return None\n",
        "WebhookTarget | str union",
    ),
    clean=lambda: (
        "def post(target: WebhookTarget, payload: dict) -> None:\n    return None\n",
        "def admit(raw: str | None) -> None:\n    return None\n",
    ),
)

RAW_URL_ESCAPES_ONLY_THROUGH_THE_DELIVERY_DOOR = Rule(
    name="raw-url-escapes-only-through-the-delivery-door",
    find=_raw_delivery_url_read_offenders,
    summary=(
        "The raw webhook URL leaves WebhookTarget only through delivery_url() at the "
        "delivery/persistence sites; a new consumer anywhere else re-opens the secret"
    ),
    bite=lambda: Bites(
        "def log_target(target: WebhookTarget) -> None:\n    print(target.delivery_url())\n",
        "delivery_url() read outside the delivery/persistence doors",
        at=APP_ROOT / "orchestration" / "request_pipeline.py",
    ),
    clean=lambda: (
        Silent(
            "def show(target: WebhookTarget) -> None:\n    print(target.redacted())\n",
            at=APP_ROOT / "orchestration" / "request_pipeline.py",
        ),
        # The path-keyed second half: a sanctioned reader stays quiet (this is
        # the shape the doors actually spell, proved at the delivery edge).
        Silent(
            "def deliver(target: WebhookTarget) -> None:\n    print(target.delivery_url())\n",
            at=_REST_API / "crawl_endpoint.py",
        ),
    ),
)

# ========================================================================= clause 2: the redaction chokepoint

#: The ONE redaction mechanism; nowhere else is credential masking spelled.
_REDACTION_CHOKEPOINT = "packages/safe-url/src/safe_url/url_redaction.py"

_SECRET_NAME_WORDS = ("token", "secret", "password", "passwd", "apikey", "api_key", "key=")
_MASK_TOKENS = ("[redacted]", "***", "*****")


def _manual_masking_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag hand-rolled credential masking outside the chokepoint: a two-arg ``.replace(...)`` whose
    arguments read as a secret name or a mask token is per-site string munging -- the law says redact_url,
    not a site-local mask.
    """
    if relative_posix(path) == _REDACTION_CHOKEPOINT:
        return []
    offenders: list[str] = []
    for node in nodes_of(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "replace" or len(node.args) != 2:
            continue
        args = [a.value if isinstance(a, ast.Constant) else None for a in node.args]
        if any(not isinstance(a, str) for a in args):
            continue
        needle, replacement = args
        assert isinstance(needle, str) and isinstance(replacement, str)
        lowered = needle.lower()
        if any(word in lowered for word in _SECRET_NAME_WORDS) or replacement.lower() in _MASK_TOKENS:
            offenders.append(
                offender_at(
                    path,
                    node,
                    f"manual credential masking {needle!r} -> {replacement!r} -- "
                    "route every redaction through safe_url.url_redaction",
                )
            )
    return offenders


MANUAL_CREDENTIAL_MASKING_IS_BANNED = Rule(
    name="manual-credential-masking-is-banned",
    find=_manual_masking_offenders,
    summary=(
        "url_redaction.redact_url is the ONE redaction mechanism; ad-hoc "
        ".replace masking of token/secret values anywhere else is a mask that "
        "will drift from the chokepoint's bypass shapes"
    ),
    bite=lambda: (
        Bites(
            'def scrub(url: str) -> str:\n    return url.replace("token", "[REDACTED]")\n',
            "manual credential masking",
        ),
        Bites(
            'def scrub(url: str) -> str:\n    return url.replace("secret", "***")\n',
            "manual credential masking",
        ),
    ),
    clean=lambda: (
        'def scrub(url: str) -> str:\n    return url.replace("http://", "https://")\n',
        'def rewrite(url: str) -> str:\n    return url.replace("old.example", "new.example")\n',
    ),
)

RULES = Registry(
    (
        RAW_WEBHOOK_URL_MOVES_THROUGH_THE_ADMITTED_TARGET,
        WEBHOOK_TARGET_IS_THE_BOUNDARY_TYPE,
        RAW_URL_ESCAPES_ONLY_THROUGH_THE_DELIVERY_DOOR,
        MANUAL_CREDENTIAL_MASKING_IS_BANNED,
    )
)


def test_no_url_secrets_rule_violations():
    """The whole app tree holds the URL-secrets law: redacted-by-default
    webhook plumbing, one redaction chokepoint, the raw URL behind delivery_url()."""
    RULES.check_all_clean()


def test_url_secrets_rules_bite():
    """Each injected violation fires with its named message; lookalikes stay quiet."""
    RULES.check_all_bite()


def test_the_gate_sweeps_the_whole_production_tree():
    """The gate is zero-exemption: every production module is in scope."""
    swept = {relative_posix(path) for path, _tree in app_trees(exclude=frozenset())}
    assert len(swept) > 200, f"the production tree sweep covered only {len(swept)} modules"
    for sanctioned in (*_SANCTIONED_RAW_STR_MODULES, _REDACTION_CHOKEPOINT):
        assert sanctioned in swept, f"sanctioned site {sanctioned} is not part of the swept tree"
