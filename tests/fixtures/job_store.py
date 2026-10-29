"""The ``job_store`` fixture: a per-test SQLite job store."""

import pytest


@pytest.fixture
def job_store(tmp_path):
    """JobStore backed by a per-test SQLite db under ``tmp_path``."""
    from tests._job_harnesses import make_job_store

    return make_job_store(str(tmp_path))
