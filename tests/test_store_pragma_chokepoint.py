"""M1: every SQLite store must share one durability configuration.

JobStore enables busy_timeout + WAL; CrawlResultStore plain-connected --
the sibling-drift class the extraction pattern already absorbed twice
(_LoopBoundPooledClient, BoundedTTLCache). Durability/concurrency settings
are a shared decision: one function configures every store connection, and
a tree-wide invariant keeps PRAGMA statements confined to it (PRAGMAs are
connection-scoped, so "configure at connect time" is a one-call contract).
"""

import ast

from _testkit import UNRESTRICTED_HOST_POLICY

from app.crawl.job_store import JobStore
from app.crawl.result_store import CrawlResultStore
from tests.chokepoint_walks import app_trees


def _pragma_exec_offenders() -> list[str]:
    """Any ``*.execute("...PRAGMA...")`` outside the sanctioned module."""
    found: list[str] = []
    for path, tree in app_trees():
        if path.name == "sqlite_store.py":
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "execute"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
                and "pragma" in node.args[0].value.lower()
            ):
                found.append(f"{path.name}:{node.lineno}")
    return found


def test_both_stores_share_the_pragma_configuration(tmp_path):
    for store in (
        JobStore(tmp_path / "jobs.db", policy=UNRESTRICTED_HOST_POLICY),
        CrawlResultStore(tmp_path / "results.db"),
    ):
        conn = store._conn
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_store_pragmas_are_confined_to_the_configurator():
    """Adding a store means reusing the chokepoint, not re-typing the
    settings -- drifting them is exactly the M1 defect."""
    offenders = _pragma_exec_offenders()
    assert not offenders, "PRAGMA outside sqlite_store.configure_store_connection: " + ", ".join(offenders)


def test_pragma_confinement_detector_bites():
    """The detector fires on the shape it bans (red-proof for the invariant)."""
    bad = ast.parse('conn.execute("PRAGMA journal_mode=WAL")')
    hits = [
        f"{n.lineno}"
        for n in ast.walk(bad)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "execute"
        and isinstance(n.args[0].value, str)
        and "pragma" in n.args[0].value.lower()
        if n.args
    ]
    assert hits == ["1"]
