"""API-suite fixtures: the loop-guard store doubles serve every offload test."""

import pytest

from tests._job_harnesses import keyed_jobs_client
from tests._job_store_doubles import LoopGuardJobStore, LoopGuardResultStore, LoopGuardWatchStore
from tests._policy_factories import UNRESTRICTED_HOST_POLICY, deployment_tier
from tests._watch_harnesses import keyed_watches_client


@pytest.fixture
def loop_guard_store(tmp_path) -> LoopGuardJobStore:
    return LoopGuardJobStore(tmp_path / "jobs.db", policy=UNRESTRICTED_HOST_POLICY, tier=deployment_tier())


@pytest.fixture
def result_store(tmp_path) -> LoopGuardResultStore:
    return LoopGuardResultStore(tmp_path / "results.db")


@pytest.fixture
def loop_guard_watch_store(tmp_path) -> LoopGuardWatchStore:
    return LoopGuardWatchStore(tmp_path / "watches.db")


@pytest.fixture
def watches_client(loop_guard_watch_store):
    return keyed_watches_client(loop_guard_watch_store)


@pytest.fixture
def jobs_client(loop_guard_store):
    return keyed_jobs_client(loop_guard_store)
