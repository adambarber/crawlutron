"""API-suite fixtures: the loop-guard store doubles serve every offload test."""

from collections.abc import Iterator

import pytest

from tests._job_harnesses import keyed_jobs_client
from tests._job_store_doubles import LoopGuardJobStore, LoopGuardResultStore, LoopGuardWatchStore
from tests._policy_factories import UNRESTRICTED_HOST_POLICY, deployment_tier
from tests._result_store_fixture import loop_permitted_store
from tests._watch_harnesses import keyed_watches_client


@pytest.fixture
def loop_guard_store(tmp_path) -> Iterator[LoopGuardJobStore]:
    store = LoopGuardJobStore(tmp_path / "jobs.db", policy=UNRESTRICTED_HOST_POLICY, tier=deployment_tier())
    try:
        yield store
    finally:
        store.close()


result_store = loop_permitted_store(lambda path: LoopGuardResultStore(path / "results.db"))


@pytest.fixture
def loop_guard_watch_store(tmp_path) -> Iterator[LoopGuardWatchStore]:
    store = LoopGuardWatchStore(tmp_path / "watches.db")
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
def watches_client(loop_guard_watch_store):
    return keyed_watches_client(loop_guard_watch_store)


@pytest.fixture
def jobs_client(loop_guard_store):
    return keyed_jobs_client(loop_guard_store)
