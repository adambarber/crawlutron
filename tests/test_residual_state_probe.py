"""Residual-state probes: no test may leak env or cwd; stores close. A leak is observable only when a later
test can SEE it: the arrange test pollutes ``os.environ`` and ``os.getcwd`` with no cleanup, and the
assertion test -- a different function, later or earlier under random ordering.
"""

import os

import pytest

_CANARY = "CRAWLUTRON_RESIDUAL_PROBE_CANARY"
_PRISTINE_ENV = dict(os.environ)
_PRISTINE_CWD = os.getcwd()


def test_residual_probe_pollutes_env_and_cwd_without_cleanup(tmp_path):
    """Arrange: leak env + cwd on purpose; the sibling test asserts restore."""
    os.environ[_CANARY] = "leaked"
    os.chdir(tmp_path)


def test_env_and_cwd_are_restored_after_the_sibling_test():
    """The contract: a polluting sibling test leaves no env or cwd residue."""
    assert _CANARY not in os.environ
    assert os.getcwd() == _PRISTINE_CWD


def test_conftest_session_fixture_restores_the_inherited_private_targets_value(
    monkeypatch,
):
    """The session loopback fixture saves the inherited value and restores IT."""
    import tests.conftest as conftest

    fixture_def = conftest._enable_loopback_for_test_suite
    fixture_function = fixture_def._fixture_function
    monkeypatch.setenv("CRAWLUTRON_ALLOW_PRIVATE_TARGETS", "7")
    generator = fixture_function()
    next(generator)
    assert os.environ["CRAWLUTRON_ALLOW_PRIVATE_TARGETS"] == "1"
    with pytest.raises(StopIteration):
        next(generator)
    assert os.environ["CRAWLUTRON_ALLOW_PRIVATE_TARGETS"] == "7"


def _watch_store_teardown_check(tmp_path) -> bool:
    """Drive the watch_store fixture by hand; True iff teardown closes it."""
    import sqlite3

    import tests.fixtures.watch_store as fixture_mod

    generator = fixture_mod.watch_store._fixture_function(tmp_path)
    store = next(generator)
    store.list_watches()
    try:
        next(generator)
    except StopIteration:
        pass
    try:
        store.list_watches()
    except sqlite3.ProgrammingError:
        return True
    return False


def test_watch_store_fixture_teardown_closes_the_store(tmp_path):
    """The watch_store fixture teardown closes the store, not GC finalization."""
    assert _watch_store_teardown_check(tmp_path)
