"""The shared selector-extraction pool sizes itself from the environment (#349)."""

import os

from crawlutron.extraction.documents import extraction_thread_count


def test_default_derives_from_cpu_with_floor_of_4(monkeypatch):
    monkeypatch.delenv("CRAWLUTRON_EXTRACTION_THREADS", raising=False)
    assert extraction_thread_count() == max(4, os.cpu_count() or 4)


def test_env_knob_overrides_the_default(monkeypatch):
    monkeypatch.setenv("CRAWLUTRON_EXTRACTION_THREADS", "7")
    assert extraction_thread_count() == 7


def test_knob_falls_back_when_malformed(monkeypatch):
    monkeypatch.setenv("CRAWLUTRON_EXTRACTION_THREADS", "bananas")
    assert extraction_thread_count() == max(4, os.cpu_count() or 4)
