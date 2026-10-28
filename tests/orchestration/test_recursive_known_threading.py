"""#381: the recursive path threads the occupied set as a value.

Each expansion unions the previous outcome's ``known_delta`` in; the
per-expansion frontier identity walk is gone from the admission seam."""

from __future__ import annotations

from tests._admission_runs import link as _link
from tests._admission_runs import run as _run
from tests._admission_runs import task as _task
from tests._policy_factories import canonical
from tests._result_builders import make_engine_result


async def test_expansions_thread_the_occupied_set(monkeypatch):
    """The expansion's ``known`` is the threaded value, not a frontier rewalk."""
    import app.admission.admission_links as admission_links

    walked: list[object] = []
    real = admission_links.frontier_identity

    def counting(value, *, host_policy):
        walked.append(value)
        return real(value, host_policy=host_policy)

    monkeypatch.setattr(admission_links, "frontier_identity", counting)

    async def worker(job):
        return make_engine_result(job.normalized_url, [_link("http://a/dup")])

    spill, _scheduler, _gate = await _run([_task("http://a/A"), _task("http://a/B", index=1)], worker)
    assert walked == [], f"the frontier was rewalked for admission: {walked}"
    # Behavior is unchanged: /dup is admitted exactly once across the discoverers.
    assert {r.normalized_url for r in spill.writes.values()} == {
        canonical("http://a/A"),
        canonical("http://a/B"),
        canonical("http://a/dup"),
    }
