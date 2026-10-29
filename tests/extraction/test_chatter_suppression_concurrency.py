"""Concurrent trafilatura chatter suppression must never pin a logger level.

Filter-based suppression mutates no level, so after suppression runs -- serially
or concurrently -- every trafilatura logger's level is exactly what it was before.
"""

import logging
import threading
from collections.abc import Callable

import pytest

from crawlutron.extraction.documents import suppress_trafilatura_chatter

TRAFILES = ("trafilatura.core", "trafilatura.utils", "trafilatura.xml")


@pytest.fixture(autouse=True)
def _level_isolation():
    """Snapshot and restore levels around each test: a failure inside an active
    suppression would otherwise leak its level into the next one."""
    loggers = [logging.getLogger(name) for name in TRAFILES]
    prior = {each: each.level for each in loggers}
    yield
    for each, level in prior.items():
        each.setLevel(level)


def _recorded_levels() -> dict[str, int]:
    return {name: logging.getLogger(name).level for name in TRAFILES}


def _run_in_threads(count: int, body: Callable[[], object]) -> list[threading.Thread]:
    threads = [threading.Thread(target=body) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return threads


def test_suppression_leaves_logger_levels_untouched():
    """A suppression that never mutates levels cannot restore the wrong one."""
    before = _recorded_levels()
    with suppress_trafilatura_chatter():
        during = _recorded_levels()
    assert _recorded_levels() == before, "the suppression changed a logger level"
    assert during == before, "suppression still mutes by level, not by filter"


def test_two_concurrent_suppressions_do_not_pin_a_raised_level():
    """Two workers suppressing together must leave the levels as they found them:
    the old snapshot/restore raced, pinning the first's CRITICAL on exit."""
    before = _recorded_levels()
    barrier = threading.Barrier(2)

    def suppress_once():
        with suppress_trafilatura_chatter():
            barrier.wait()

    _run_in_threads(2, suppress_once)
    assert _recorded_levels() == before, "concurrent suppressions permanently raised a logger level"


def test_suppression_actually_silences_chatter(caplog):
    """The point of the suppression: a trafilatura ERROR inside the block must
    not reach a root handler while suppression is active."""
    chatter = logging.getLogger("trafilatura.core")
    chatter.setLevel(logging.DEBUG)
    with suppress_trafilatura_chatter():
        chatter.error("benign-input chatter")
    assert not caplog.records, "suppression silenced nothing"


def test_chatter_after_suppression_is_visible_again(caplog):
    """And the converse: once the block exits, an ERROR must be loggable again.
    A suppression that silenced by pinning CRITICAL would fail this."""
    chatter = logging.getLogger("trafilatura.core")
    chatter.setLevel(logging.DEBUG)
    with suppress_trafilatura_chatter():
        pass
    with caplog.at_level(logging.DEBUG, logger="trafilatura.core"):
        chatter.error("real failure")
    assert caplog.records, "an error after the suppression block stayed silenced"
