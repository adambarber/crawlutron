"""One pytest fixture factory: a store whose sync seeds may run on the event loop."""

from __future__ import annotations

import pathlib
from collections.abc import Callable, Generator

import pytest

from crawlutron.storage.coordination_store import CoordinationStore
from crawlutron.storage.safe_db import allow_on_loop


def loop_permitted_store[S: CoordinationStore](
    build: Callable[[pathlib.Path], S],
) -> Callable[[pathlib.Path], Generator[S, None, None]]:
    """Fixture factory: a store from ``build`` whose sync seeds may run on the loop."""

    @pytest.fixture
    def store_fixture(tmp_path: pathlib.Path) -> Generator[S, None, None]:
        store = build(tmp_path)
        with allow_on_loop():
            yield store
        store.close()

    return store_fixture
