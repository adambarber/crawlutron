"""An ownable spill or none: a failed ownership lock is fatal (#C7).

A spill IS the crawl's result. _claim_ownership logged the flock failure and
continued -- and gc_stale_spills' probe (taking the lock IS the ownership test)
then acquires the lock the live spill failed to take and DELETES the file after
gc_age_seconds. A long crawl with a failed lock lost its spill.
"""

import fcntl

import pytest

from crawlutron.orchestration.engine import ResultSpill


def test_a_failed_ownership_lock_is_fatal(monkeypatch, tmp_path):
    """The flock's refusal must refuse the spill: construction raises instead of
    leaving an ownable-by-anyone file the sweep later collects."""
    real_flock = fcntl.flock

    def refusing_flock(fd, operation):
        if operation & fcntl.LOCK_NB and operation & fcntl.LOCK_EX:
            raise OSError("flock refused (simulated)")
        return real_flock(fd, operation)

    monkeypatch.setattr(fcntl, "flock", refusing_flock)

    with pytest.raises(OSError, match="could not be owned"):
        ResultSpill(path=str(tmp_path / "spill.ndjson"))
