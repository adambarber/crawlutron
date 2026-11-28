"""Cookie-slot retention evicts the oldest host, never the just-written one."""

from __future__ import annotations

import itertools
from contextlib import closing
from pathlib import Path

import pytest

from crawlutron.storage import coordination_cookies
from crawlutron.storage.coordination_cookies import SharedCookieStore


def test_merge_keeps_newest_host_at_capacity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With identical clocks, the lexically smallest host is the eviction victim."""
    monkeypatch.setattr(coordination_cookies.time, "time", lambda: 1000.0)
    with closing(SharedCookieStore(tmp_path / "cookies.db", max_hosts=2)) as store:
        for host in ("a.example", "b.example"):
            store.merge("", host, {"sid": host})

        store.merge("", "c.example", {"sid": "c.example"})

        assert set(store.slots_for_namespace("")) == {"b.example", "c.example"}


def test_merge_evicts_the_least_recently_used_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Touching an old slot moves it past the retention boundary, not into it."""
    clocks = itertools.count(1000.0, 1.0)
    monkeypatch.setattr(coordination_cookies.time, "time", lambda: next(clocks))
    with closing(SharedCookieStore(tmp_path / "cookies.db", max_hosts=2)) as store:
        store.merge("", "a.example", {"sid": "a"})
        store.merge("", "b.example", {"sid": "b"})
        store.merge("", "a.example", {"sid": "a2"})

        store.merge("", "c.example", {"sid": "c"})

        assert set(store.slots_for_namespace("")) == {"a.example", "c.example"}
