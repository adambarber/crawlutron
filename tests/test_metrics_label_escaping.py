"""Label values in the Prometheus exposition are escaped: a quote, backslash
or newline inside a value must not end a label early or inject a phantom
sample, and parsing the exposition still yields one sample per family."""

from __future__ import annotations

import pytest

from crawlutron.runtime.metrics import MetricsStore
from crawlutron.storage.coordination_counters import CoordinationCounters

_CALLER = "crawler_crawl_requests_by_caller_total"
_PROVIDER = "crawler_search_results_by_provider_total"


@pytest.mark.parametrize(
    "value, rendered",
    [
        ('a"b', 'a\\"b'),
        ("a\\b", "a\\\\b"),
        ("a\nb", "a\\nb"),
        ('a"b\\c\nd', 'a\\"b\\\\c\\nd'),
    ],
)
async def test_caller_label_value_escapes_special_characters(tmp_path, value, rendered):
    """A hostile caller id cannot corrupt the scrape: the value renders escaped
    and the exposition still holds exactly one sample for the family."""
    store = MetricsStore(counters=CoordinationCounters(tmp_path / "esc.db"))
    await store.observe_crawl(caller_id=value)
    text = await store.as_prometheus(caller_detail=True)
    lines = [line for line in text.splitlines() if line.startswith(_CALLER + "{")]
    assert len(lines) == 1, f"exactly one sample per family, got {lines!r}"
    assert lines[0] == f'{_CALLER}{{caller="{rendered}"}} 1.0'


async def test_provider_label_value_escapes_special_characters(tmp_path) -> None:
    """The shared escape helper covers every labelled family, providers included."""
    store = MetricsStore(counters=CoordinationCounters(tmp_path / "esc.db"))
    await store.observe_search(provider='br"ave\\x\n', results=1)
    text = await store.as_prometheus(caller_detail=True)
    lines = [line for line in text.splitlines() if line.startswith(_PROVIDER + "{")]
    assert len(lines) == 1
    assert lines[0] == f'{_PROVIDER}{{provider="br\\"ave\\\\x\\n"}} 1.0'
