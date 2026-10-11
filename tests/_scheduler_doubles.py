"""Scheduler doubles, and the one way a test should build or drive a scheduler.

Three things lived at a dozen call sites and belong here. The constructor: a
scheduler test that hand-rolls it re-spells the same three knobs -- one job at a
time globally and per domain, no fixed delay -- so the pairs of them are the
pairs that drift, and a test that needs ``circuit_cooldown=0`` silently
re-asserts production's 600-second default everywhere it does not ask for it.
The failure envelope: the shape a broken job is handed back is the crawl
pipeline's, and it is the pipeline's own ``failed_result_for`` that decides it.
The delivery channel: ``on_result`` is where a crawl spills a result to disk, so
"the sink misbehaved" is a scenario several tests set up, and each of them was
spelling the same callback, the same log capture and the same run call.
"""

from typing import TypeVar

from app.models.crawl_job import CrawlJob
from app.models.crawl_result import EngineCrawlResult
from app.models.crawl_task import CrawlTask
from app.orchestration.result_helpers import failed_result_for
from app.orchestration.scheduler import DomainScheduler
from tests._result_builders import make_result

# The two job shapes ``DomainScheduler.run`` is polymorphic over. The doubles here
# serve both, so they are generic over exactly that pair -- a bound TypeVar
# annotated with one of the two would have been a lie the scheduler's own
# ``ScheduledJob`` docstring warns about.
_EnvelopeJob = TypeVar("_EnvelopeJob", CrawlJob, CrawlTask)


def _job_url(job: CrawlJob | CrawlTask) -> str:
    """The reporting URL as TEXT, whichever of the two job shapes carries it.

    ``ScheduledJob`` deliberately has no URL member: the two shapes name it
    differently, so reading it is the pipeline boundary's job, not the
    scheduler's. A ``CrawlJob`` holds the admitted value rather than a string.
    """
    if isinstance(job, CrawlTask):
        return job.original_url
    return job.url.raw


# The three knobs a scheduling test always wants and no scheduling test ever
# wants spelled out: serial execution makes ordering observable, and a zero
# fixed delay keeps the suite off a real wall clock. Everything else -- the
# circuit threshold, the cooldown, the token bucket, the adaptive bounds -- stays
# on production's default, read from the constructor this module calls.
_SERIAL_UNTHROTTLED = {"global_limit": 1, "per_domain_limit": 1, "delay_seconds": 0.0}


def make_scheduler(of: type[DomainScheduler] = DomainScheduler, **overrides) -> DomainScheduler:
    """A serial, undelayed scheduler; ``overrides`` name any other knob.

    ``of`` is there for a scheduler a test specializes (a fixed per-domain delay,
    say): the subclass is still built through the shared defaults rather than
    re-spelling them a second time.
    """
    return of(**{**_SERIAL_UNTHROTTLED, **overrides})


def trip_circuit(scheduler: DomainScheduler, domain: str, *, failures: int | None = None) -> None:
    """Record consecutive failures for ``domain`` until the circuit opens.

    ``failures`` defaults to the scheduler's own ``_circuit_threshold``, so the
    number of failures that opens the breaker is read from the constructor the
    scheduler was built with rather than restated at every call site.
    """
    for _ in range(scheduler._circuit_threshold if failures is None else failures):
        scheduler.record_failure(domain)


async def ok_worker[EnvelopeJob: (CrawlJob, CrawlTask)](job: EnvelopeJob) -> EngineCrawlResult:
    """A worker that succeeds, returning a real ``SchedulerResult``.

    A stand-in that returned a bare ``"ok"`` would not be one: the scheduler
    reads ``.status`` off whatever the worker returns, so a string is a domain
    failure raised inside the scheduler and then swallowed into a fabricated
    envelope -- a test whose worker "succeeded" while the scheduler recorded the
    opposite.
    """
    return make_result(_job_url(job), job.domain)


def engine_envelope[EnvelopeJob: (CrawlJob, CrawlTask)](
    job: EnvelopeJob, error: BaseException
) -> EngineCrawlResult:
    """The crawl pipeline's real failure envelope.

    Routes through the production ``failed_result_for`` chokepoint rather than
    re-building an envelope here: the classification, redaction and
    ``normalized_url`` fallback are that function's contract, and a test-local
    copy would silently drift from it (it already did twice). The URL accessor
    is supplied here for the same reason production supplies one -- the two job
    shapes carry it under different names.
    """
    return failed_result_for(job, error, url_of=_job_url)


class FailingSink:
    """An ``on_result`` sink whose delivery fails, recording what it was handed.

    ``calls`` is every delivery the scheduler attempted -- so a test can prove
    one call per job and no retry -- and ``delivered`` the calls that actually
    completed, which is how a test tells "this job's spill failed" from "this
    job's result was lost". ``fails`` is the set of job indices whose delivery
    raises; every one of them by default.
    """

    def __init__(self, *, message: str = "spill failed", fails: set[int] | None = None) -> None:
        self.message = message
        self.fails = fails
        self.calls: list[tuple[int, str]] = []
        self.delivered: list[tuple[int, str]] = []

    async def __call__(self, index: int, result) -> None:
        self.calls.append((index, result.status))
        if self.fails is None or index in self.fails:
            raise OSError(self.message)
        self.delivered.append((index, result.status))


async def run_through(sink, scheduler: DomainScheduler, jobs, caplog) -> None:
    """Submit ``jobs`` with ``sink`` as the only delivery channel, capturing logs.

    The capture is part of the scenario rather than the test's bookkeeping: a
    swallowed delivery failure is asserted through the log line it left, so a
    run that stopped capturing could not tell the two apart.
    """
    with caplog.at_level("ERROR"):
        await scheduler.run(jobs, ok_worker, on_result=sink, failed_result=engine_envelope)


class FakeScheduler:
    def __init__(self, pins=None):
        self.runs = 0
        self.pins: dict[str, str] = dict(pins or {})

    def get_pinned_strategy(self, domain: str):
        return self.pins.get(domain)

    async def run(self, jobs, worker, on_result=None, *, failed_result=None):
        self.runs += 1
        out = {}
        for job in jobs:
            try:
                out[job.index] = await worker(job)
            except Exception as error:
                if failed_result is None:
                    raise
                out[job.index] = failed_result(job, error)
        return out


__all__ = [
    "FailingSink",
    "FakeScheduler",
    "engine_envelope",
    "make_scheduler",
    "ok_worker",
    "run_through",
    "trip_circuit",
]
