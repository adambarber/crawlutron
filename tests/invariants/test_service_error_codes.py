"""Service raise sites and SDK error classes must agree on the wire code and status."""

from __future__ import annotations

import ast
import pathlib

import crawlutron_sdk
from crawlutron.service.errors import DomainError, JobStateConflict

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

#: Live raise-site sources the wire triplets are re-derived from each run.
_RAISE_SITE_SOURCES = (
    "crawlutron/api/crawl_endpoint.py",
    "crawlutron/service/errors.py",
)

_DISCONNECT_SOURCE = "crawlutron/api/crawl_endpoint.py"


#: Raise sites whose wire code is not a literal, named and reasoned so the AST
#: derivation cannot silently skip them. Each row's code is chosen by the CALLER.
_NON_LITERAL_CODE_SITES: dict[str, str] = {
    "crawlutron/service/errors.py:DomainError": "base constructor; subclasses supply the triplet",
    "crawlutron/service/errors.py:ValidationRefusal": "422 triplet forwarded from the validation caller",
    "crawlutron/service/errors.py:AuthRefusal": "401 code forwarded from the auth caller",
}


def _module_sources() -> dict[str, str]:
    return {name: (REPO_ROOT / name).read_text() for name in _RAISE_SITE_SOURCES}


def _literal_arg(node: ast.AST) -> str | int | None:
    value = node.value if isinstance(node, ast.Constant) else None
    if isinstance(value, str):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _triplet_args(node: ast.Call) -> tuple[str | int | None, str | int | None]:
    """(status, code) from a raise call: positional 0/1, else keyword fallback."""
    status = _literal_arg(node.args[0]) if node.args else None
    code = _literal_arg(node.args[1]) if len(node.args) > 1 else None
    keywords = {kw.arg: _literal_arg(kw.value) for kw in node.keywords if kw.arg is not None}
    return status or keywords.get("status"), code or keywords.get("code")


def _is_raise_call(node: ast.Call) -> bool:
    func = node.func
    if getattr(func, "id", None) == "http_error":
        return True
    if isinstance(func, ast.Attribute) and func.attr == "__init__":
        target = func.value
        return isinstance(target, ast.Call) and getattr(target.func, "id", None) == "super"
    return False


def _raise_sites(tree: ast.AST, source_name: str) -> list[tuple[str, int, str]]:
    """Literal (code, status, source) triplets; non-literal sites become sentinels."""
    triplets: list[tuple[str, int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_raise_call(node):
            continue
        status, code = _triplet_args(node)
        if isinstance(code, str) and isinstance(status, int):
            triplets.append((code, status, source_name))
        else:
            enclosing = _enclosing_class_name(tree, node)
            triplets.append((f"{source_name}:{enclosing or f'line{node.lineno}'}", -1, source_name))
    return triplets


def _http_error_calls(tree: ast.AST) -> list[ast.Call]:
    """Every Call shaped like http_error, however the name was bound:
    direct import, Assign/ImportFrom alias, or attribute form. Matches
    CALL SHAPE, not binding resolution; classification via _triplet_args."""
    aliased = (
        {
            node.targets[0].id
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Name)
            and "http_error" in ast.unparse(node.value)
        }
        | {
            alias.asname
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
            if alias.name == "http_error" and alias.asname
        }
        | {"http_error"}
    )
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            getattr(node.func, "id", None) in aliased
            or (isinstance(node.func, ast.Attribute) and node.func.attr == "http_error")
        )
    ]


def _init_raise_count(tree: ast.AST) -> int:
    """__init__ bodies that chain to a DomainError-style base via any local
    alias: the census counts the body, so renames cannot dodge it."""
    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "__init__":
            continue
        locals_bound = {
            t.id
            for stmt in ast.walk(node)
            if isinstance(stmt, ast.Assign) and isinstance(stmt.targets[0], ast.Name)
            for t in [stmt.targets[0]]
            if "super" in ast.unparse(stmt.value)
        }
        calls = [c for c in ast.walk(node) if isinstance(c, ast.Call)]
        if any(
            isinstance(c.func, ast.Attribute)
            and c.func.attr == "__init__"
            or (isinstance(c.func, ast.Name) and c.func.id in locals_bound)
            for c in calls
        ):
            count += 1
    return count


def _raise_census() -> tuple[int, int]:
    """(classified, total) raise sites: total counts every http_error-shaped call
    plus every base-chaining __init__ body, alias-proof by construction."""
    classified = len(_wire_triplets())
    total = 0
    for text in _module_sources().values():
        tree = ast.parse(text)
        total += len(_http_error_calls(tree))
        total += _init_raise_count(tree)
    return classified, total


def _enclosing_class_name(tree: ast.AST, node: ast.Call) -> str | None:
    for branch in ast.walk(tree):
        if not isinstance(branch, ast.ClassDef):
            continue
        if any(isinstance(child, ast.Call) and child is node for child in ast.walk(branch)):
            return branch.name
    return None


def _wire_triplets() -> list[tuple[str, int, str]]:
    """(code, status, source) for every AST-literal raise site; non-literal
    sites land as ``<source>:<ClassOrLine>`` sentinels the sweep asserts exempted."""
    triplets: list[tuple[str, int, str]] = []
    for source_name, text in _module_sources().items():
        triplets.extend(_raise_sites(ast.parse(text), source_name))
    return triplets


def _sdk_classes_for(code: str) -> list[str]:
    return [
        name
        for name in crawlutron_sdk.errors.__all__
        if name.endswith("Error") and getattr(crawlutron_sdk, name).code == code
    ]


def _assert_pair(refusal: DomainError, sdk_class: type[crawlutron_sdk.CrawlutronError], origin: str) -> None:
    assert refusal.code == sdk_class.code, (
        f"{origin}: the service raises code {refusal.code!r} (status {refusal.status}) but the SDK "
        f"class {sdk_class.__name__} carries {sdk_class.code!r} -- the SDK constant drifted from "
        f"the live service raise site; update crawlutron_sdk/errors.py in the same change."
    )
    assert refusal.status == sdk_class.status_code, (
        f"{origin}: the service answers status {refusal.status} for code {refusal.code!r} but the "
        f"SDK class {sdk_class.__name__} declares status_code={sdk_class.status_code}."
    )


def test_conflict_refusal_matches_the_sdk_conflict_class():
    """JobStateConflict IS the 409/invalid_state service refusal the SDK mirrors."""
    _assert_pair(JobStateConflict("stale"), crawlutron_sdk.CrawlutronConflictError, "JobStateConflict")


def test_client_disconnected_refusal_matches_the_sdk_class():
    """Re-derive the 499 triplet from the raise-site AST, then pin it against the SDK."""
    tree = ast.parse(_module_sources()[_DISCONNECT_SOURCE])
    calls = [
        node
        for node in _http_error_calls(tree)
        if isinstance(node.args[0], ast.Constant) and node.args[0].value == 499
    ]
    assert len(calls) == 1, (
        f"expected exactly-one http_error(499, ...) raise site in {_DISCONNECT_SOURCE}, found "
        f"{len(calls)} -- the disconnect refusal moved; re-derive this pin from the new site."
    )
    code = calls[0].args[1]
    assert isinstance(code, ast.Constant), f"499 raise site code must be a string literal, got {code}"
    sdk_class = crawlutron_sdk.CrawlutronClientClosedRequestError
    assert code.value == sdk_class.code, (
        f"{_DISCONNECT_SOURCE} raises 499 with code {code.value!r} but {sdk_class.__name__} carries "
        f"{sdk_class.code!r} -- the SDK constant drifted from the live service raise site."
    )
    assert sdk_class.status_code == 499


def test_wire_triplets_carry_multiline_raise_sites():
    """The sweep re-derives triplets the regex sweep cannot see: raise calls split
    across lines are invisible to a single-line regex but not to the AST."""
    triplets = set(_wire_triplets())
    assert ("capacity_exceeded", 503, "crawlutron/service/errors.py") in triplets
    assert ("pending", 202, _DISCONNECT_SOURCE) in triplets


def test_wire_triplets_name_every_non_literal_code_site():
    """Non-literal raise sites must all be explicit exemptions.
    Classified + exempted must equal the site count: a routed-around call
    (aliased super, renamed helper) would otherwise vanish silently."""
    triplets = _wire_triplets()
    sentinels = [code for code, status, _ in triplets if not isinstance(status, int) or status < 0]
    assert set(sentinels) == set(_NON_LITERAL_CODE_SITES), (
        f"non-literal raise sites {set(sentinels) ^ set(_NON_LITERAL_CODE_SITES)} disagree with "
        f"the exemption table -- name each site with a reason or make the code literal."
    )
    classified, total = _raise_census()
    assert classified == total, (
        f"the sweep classified {classified} raise sites but the sources carry {total} "
        f"raise-shaped calls -- a call shape is escaping the derivation (aliased super, "
        f"renamed helper); extend _is_raise_call or exempt the site by name."
    )


def test_wire_codes_shared_with_the_sdk_agree_on_status():
    """AST-derived literal raise sites whose code an SDK class also carries:
    exactly one class, statuses agree; non-literal sites are exempted by name."""
    for code, status, source in _wire_triplets():
        if not isinstance(status, int) or status < 0:
            assert code in _NON_LITERAL_CODE_SITES, (
                f"{source} raises a non-literal wire code the sweep cannot derive; "
                f"name it in _NON_LITERAL_CODE_SITES with a reason or make it literal."
            )
            continue
        classes = _sdk_classes_for(code)
        if not classes:
            continue
        assert len(classes) == 1, (
            f"{source} raises wire code {code!r} at status {status}; expected exactly one SDK "
            f"class carrying it, found {classes}"
        )
        sdk_class = getattr(crawlutron_sdk, classes[0])
        assert sdk_class.status_code == status, (
            f"{source} answers status {status} for wire code {code!r} but the SDK class "
            f"{classes[0]} declares status_code={sdk_class.status_code}."
        )


def _census_from_source(text: str) -> int:
    return len(_http_error_calls(ast.parse(text)))


def test_census_sees_attribute_form_http_error_calls():
    """module.http_error(...) has an ast.Attribute func; the census matches it
    regardless of how the module binding arrived, so it cannot dodge."""
    text = "import m\n\ndef f():\n    m.http_error(404, 'gone')\n"
    assert _census_from_source(text) == 1


def test_census_sees_import_alias_bindings():
    """from x import http_error as he: the alias is an ast.ImportFrom name,
    not an ast.Assign, so the old alias-learning never saw it."""
    text = "from x import http_error as he\n\ndef f():\n    he(500, 'bad')\n"
    assert _census_from_source(text) == 1


def test_census_sees_aliased_attribute_combination():
    """Nested combination (from x import y as z; z.http_error(...)): the census
    matches the attribute form without tracking the base binding -- every
    http_error-shaped call is seen by census; classification stays with args."""
    text = "from x import y as z\n\ndef f():\n    z.http_error(500, 'bad')\n"
    assert _census_from_source(text) == 1


def test_census_widening_leaves_the_live_tree_derived_set_unchanged():
    """The widening is provably non-behavioral on the real tree: the live
    sources carry exactly the one plain-import call, no new sites appear."""
    plain = sum(
        sum(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "http_error"
            for n in ast.walk(ast.parse(text))
        )
        for text in _module_sources().values()
    )
    live = sum(_census_from_source(text) for text in _module_sources().values())
    assert live == plain, (
        f"the widened census counts {live} http_error calls across the live sources but only "
        f"{plain} plain-name sites exist -- the widening introduced new sites; re-derive this pin."
    )
