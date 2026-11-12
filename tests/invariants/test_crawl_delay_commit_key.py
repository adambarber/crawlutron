"""The crawl-delay commit seam: a site-keyed commit is the only spelling (#456)."""

from __future__ import annotations

import ast
import pathlib

from tests.chokepoint_walks import (
    APP_ROOT,
    call_nodes,
    local_nodes,
    offender_at,
    resolved_call_name,
)
from tests.rules import Bites, Rule

#: The seam: its key parameter must be the scheduler domain key (a ``site``),
#: never a bare host or URL -- #456's defect class.
_SEAM_NAME = "commit_crawl_delay"

#: The one file whose seam parameter the law constrains directly.
_SEAM_MODULE = APP_ROOT / "admission" / "admission_links.py"


def _site_keyed(node: ast.AST, tree: ast.Module) -> bool:
    """Whether ``node`` is a ``.site`` (or ``scheduler_domain_key``) derivation."""
    if isinstance(node, ast.Call):
        return resolved_call_name(tree, node).endswith("scheduler_domain_key")
    return isinstance(node, ast.Attribute) and node.attr == "site"


def _off_seam_key_offenders(tree: ast.Module, path: pathlib.Path) -> list[str]:
    """Every commit_crawl_delay call whose key is not a derived site key."""
    offenders: list[str] = []
    for call in call_nodes(tree):
        if resolved_call_name(tree, call).rsplit(".", 1)[-1] != _SEAM_NAME:
            continue
        if not call.args or call.keywords:
            offenders.append(offender_at(path, call, f"{_SEAM_NAME} called without a positional site key"))
            continue
        if _site_keyed(call.args[0], tree):
            continue
        offenders.append(
            offender_at(
                path,
                call.args[0],
                f"{_SEAM_NAME} key is not site-derived: '{ast.unparse(call.args[0])}' "
                "writes a Crawl-delay where the scheduler never reads it (#456)",
            )
        )
    return offenders


def _off_seam_host_commit_offenders(tree: ast.Module, path: pathlib.Path) -> list[str]:
    """The law's second half, from the definitions side: the seam module must
    derive the key from the candidate URL, not from a bare host attribute."""
    if path != _SEAM_MODULE:
        return []
    offenders: list[str] = []
    for node in local_nodes(tree):
        if not isinstance(node, ast.Call) or len(node.args) != 2:
            continue
        callee = resolved_call_name(tree, node)
        if not callee.endswith(_SEAM_NAME) or callee.rsplit(".", 1)[-1] != _SEAM_NAME:
            continue
        key = node.args[0]
        if isinstance(key, ast.Attribute) and key.attr in {"host", "raw", "origin"}:
            offenders.append(
                offender_at(
                    path,
                    key,
                    f"{_SEAM_NAME} committed under a host-keyed 'candidate.raw': "
                    "the scheduler reads the site key only (#456)",
                )
            )
    return offenders


COMMIT_CRAWL_DELAY_IS_SITE_KEYED = Rule(
    name="commit-crawl-delay-is-site-keyed",
    find=lambda tree, path: _off_seam_key_offenders(tree, path) + _off_seam_host_commit_offenders(tree, path),
    summary=(
        "every commit_crawl_delay call site must derive its key via "
        "NormalizedUrl.site (or scheduler_domain_key); a host-keyed commit "
        "writes a crawl delay where nothing reads it (#456)"
    ),
    bite=lambda: (
        Bites(
            "def admit(candidate, commit_crawl_delay):\n    commit_crawl_delay(candidate.host, 1.0)\n",
            "candidate.host",
        ),
        Bites(
            "import crawlutron.admission.admission_links as m\n"
            "def f(pass_, candidate, delay):\n"
            "    m._admit_chunk_call_site(pass_, candidate.normalized_url.host, delay)\n"
            "commit_crawl_delay(candidate.raw, 1.0)\n",
            "candidate.raw",
        ),
    ),
    clean=lambda: (
        "def admit(candidate, commit_crawl_delay):\n"
        "    commit_crawl_delay(candidate.normalized_url.site, 1.0)\n",
        "def plan(domain, delay, commit_crawl_delay):\n"
        "    commit_crawl_delay(scheduler_domain_key(task), delay)\n",
        "def other(candidate, commit_crawl_delay, delay):\n"
        "    commit_crawl_delay(candidate.normalized_url.site, delay)\n"
        "    unrelated(candidate.host, 1.0)\n",
    ),
)


def test_commit_crawl_delay_call_sites_are_site_keyed():
    """The #456 residual: a host-keyed commit through the seam must fail this gate."""
    COMMIT_CRAWL_DELAY_IS_SITE_KEYED.scan()
