"""Scheduler doubles, and the one way a test should build or drive a scheduler.

Three things lived at a dozen call sites: the constructor knobs, the failure
envelope (``failed_result_for``), and the ``on_result`` delivery channel."""

from typing import TypeVar

from app.models.crawl_job import CrawlJob
from app.models.crawl_result import EngineCrawlResult
from app.models.crawl_task import CrawlTask
from app.orchestration.results import failed_result_for
from app.transport.scheduler import DomainScheduler
from tests._result_builders import make_result

# The two job shapes ``DomainScheduler.run`` is polymorphic over.
_EnvelopeJob = TypeVar("_EnvelopeJob", CrawlJob, CrawlTask)


def _job_url(job: CrawlJob | CrawlTask) -> str:
    """The reporting URL as TEXT, whichever of the two job shapes carries it.

    The two shapes name the member differently, so reading it is the pipeline
    boundary's job, not the scheduler's; ``CrawlJob`` holds the admitted value."""
    if isinstance(job, CrawlTask):
        return job.original_url
    return job.url.raw


# The three knobs a scheduling test always wants and no scheduling test ever wants spelled out: serial
# execution makes ordering observable, and a zero fixed delay keeps the suite off a real wall clock.
_SERIAL_UNTHROTTLED = {"global_limit": 1, "per_domain_limit": 1, "delay_seconds": 0.0}


def make_scheduler(of: type[DomainScheduler] = DomainScheduler, **overrides) -> DomainScheduler:
    """A serial, undelayed scheduler; ``overrides`` name any other knob.

    ``scheduler_of`` exists for a specialized subclass -- still built through
    the shared defaults rather than re-spelling them a second time."""
    return of(**{**_SERIAL_UNTHROTTLED, **overrides})


def trip_circuit(scheduler: DomainScheduler, domain: str, *, failures: int | None = None) -> None:
    """Record consecutive failures for ``domain`` until the circuit opens.

    ``failures`` defaults to the scheduler's ``circuit_threshold``, so the
    count that opens the breaker is read from the built constructor."""
    for _ in range(scheduler.circuit_threshold if failures is None else failures):
        scheduler.record_failure(domain)


async def ok_worker[EnvelopeJob: (CrawlJob, CrawlTask)](job: EnvelopeJob) -> EngineCrawlResult:
    """A worker that succeeds, returning a real ``SchedulerResult``.

    A bare ``"ok"`` would not be one: the scheduler reads ``.status`` off the
    return, so a string becomes a swallowed domain failure."""
    return make_result(_job_url(job), job.domain)


def engine_envelope[EnvelopeJob: (CrawlJob, CrawlTask)](
    job: EnvelopeJob, error: BaseException
) -> EngineCrawlResult:
    """The crawl pipeline's real failure envelope.

    Routes through production ``failed_result_for`` so classification, redaction
    and the URL fallback stay its contract -- a local copy drifted twice."""
    return failed_result_for(job, error, url_of=_job_url)


class FailingSink:
    """An ``on_result`` sink whose delivery fails, recording what it was handed.

    ``calls``/``delivered`` are attempted vs completed (how a test tells
    "spill failed" from "result lost"); ``fails`` is the indices that raise."""

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

    The capture is part of the scenario: a swallowed delivery failure is
    asserted through the log line it left."""
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
