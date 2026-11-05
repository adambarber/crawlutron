"""One scheduler construction home: DomainScheduler.from_settings (#482, #142)."""

import ast
from collections.abc import Callable

from crawlutron.config.settings import CrawlerSettings
from crawlutron.transport.scheduler import DomainScheduler

_KNOBS = (
    "_per_domain_limit",
    "_delay_seconds",
    "_max_rps",
    "_adaptive",
    "_adaptive_min_delay",
    "_adaptive_max_delay",
    "_adaptive_latency_factor",
)


def _admission_scheduler(policy_src: dict[str, object], settings: CrawlerSettings) -> DomainScheduler:
    from crawlutron.admission.admission import JobAdmissionContext
    from crawlutron.admission.policy import PolicyTier
    from crawlutron.models.crawl_policy import CrawlPolicy

    policy = CrawlPolicy.model_validate(policy_src)
    context = JobAdmissionContext.create(policy, PolicyTier.model_defaults(), settings)
    return context.scheduler


def test_both_paths_agree_on_scheduler_config():
    settings = CrawlerSettings.from_mapping(
        {
            "CRAWLUTRON_PER_DOMAIN_CONCURRENCY": "3",
            "CRAWLUTRON_PER_DOMAIN_DELAY_MS": "750",
            "CRAWLUTRON_MAX_RPS": "4.5",
            "CRAWLUTRON_ADAPTIVE_THROTTLING": "false",
            "CRAWLUTRON_ADAPTIVE_MIN_DELAY": "0.2",
            "CRAWLUTRON_ADAPTIVE_MAX_DELAY": "12.0",
            "CRAWLUTRON_ADAPTIVE_LATENCY_FACTOR": "1.5",
        }
    )
    admission = _admission_scheduler({"limits": {"max_rps": 4.5}}, settings)
    pipeline = DomainScheduler.from_settings(settings, global_limit=8, max_rps=4.5)
    knobs = {knob: getattr(admission, knob) for knob in _KNOBS}
    assert knobs == {knob: getattr(pipeline, knob) for knob in _KNOBS}, knobs


def test_settings_default_not_getattr_default():
    """A knob added to CrawlerSettings must reach both paths; the drift this
    guards was admission's getattr default diverging from the settings default."""
    settings = CrawlerSettings.from_mapping({})
    admission = _admission_scheduler({}, settings)
    assert admission._adaptive_max_delay == settings.adaptive_max_delay


def _render_scheduler_of(settings: CrawlerSettings) -> DomainScheduler:
    from crawlutron.api.render import render_scheduler

    return render_scheduler(settings)


def test_render_scheduler_builds_through_from_settings():
    """#142: /v1/render must construct its scheduler through the one declared
    path, not a parallel DomainScheduler(...) call. Render has no policy
    carrier, so max_rps stays the service posture (0.0), but the adaptive-knob
    settings home must reach the batch: an operator's
    CRAWLUTRON_ADAPTIVE_THROTTLING=false is honored on /v1/render too."""
    settings = CrawlerSettings.from_mapping(
        {
            "CRAWLUTRON_GLOBAL_CONCURRENCY": "7",
            "CRAWLUTRON_PER_DOMAIN_CONCURRENCY": "3",
            "CRAWLUTRON_PER_DOMAIN_DELAY_MS": "750",
            "CRAWLUTRON_ADAPTIVE_THROTTLING": "false",
            "CRAWLUTRON_ADAPTIVE_MAX_DELAY": "12.0",
        }
    )
    scheduler = _render_scheduler_of(settings)
    pipeline = DomainScheduler.from_settings(settings, global_limit=7, max_rps=0.0)
    knobs = {knob: getattr(scheduler, knob) for knob in _KNOBS}
    expected = {knob: getattr(pipeline, knob) for knob in _KNOBS}
    expected.update({"_global_limit": pipeline.global_limit, "_max_rps": pipeline._max_rps})
    knobs.update({"_global_limit": scheduler.global_limit, "_max_rps": scheduler._max_rps})
    assert knobs == expected, knobs


def test_render_scheduler_global_limit_reads_settings_home():
    """#142: the render scheduler's global_limit is the settings value, not a
    re-defaulted getattr -- the settings default is the one number."""
    settings = CrawlerSettings(global_concurrency=9)
    scheduler = _render_scheduler_of(settings)
    assert scheduler.global_limit == 9


def test_admission_global_limit_not_getattr_redefaulted():
    """#142: admission passes the settings value straight through -- the
    getattr(..., 4) re-default is the drift from_settings exists to stop."""
    settings = CrawlerSettings.from_mapping({"CRAWLUTRON_GLOBAL_CONCURRENCY": "6"})
    scheduler = _admission_scheduler({"limits": {"max_rps": 4.5}}, settings)
    assert scheduler.global_limit == 6


def test_recovery_start_limit_not_getattr_redefaulted():
    """#142: boot recovery sizes its runner cap from the settings home, not a
    second getattr default that can drift from CrawlerSettings.global_concurrency."""
    from crawlutron.api.job_recovery import recovery_start_limit

    assert recovery_start_limit(CrawlerSettings(global_concurrency=6)) == 6


def _app_call_offenders(skip_suffix: str, bites: Callable[[ast.Call, object], str | None]) -> list[str]:
    """File:line for every app call ``bites`` flags, outside ``skip_suffix`` modules.

    Shared sweep for the #142 structural gates: the bypass shape is mechanical,
    so one walker serves both detectors."""
    from tests.chokepoint_walks import app_trees, call_nodes, resolved_call_name

    offenders = []
    for path, tree in app_trees(exclude=frozenset()):
        rel = str(path)
        if rel.endswith(skip_suffix):
            continue
        for node in call_nodes(tree):
            if bites(node, resolved_call_name(tree, node)):
                offenders.append(f"{rel}:{getattr(node, 'lineno', 1)}")
    return offenders


def test_domain_scheduler_constructed_only_in_its_home():
    """#142: ``DomainScheduler(...)`` is constructed nowhere but
    crawlutron/transport/scheduler.py; every other module goes through
    ``from_settings``, the declared single construction path."""

    def direct_build(_node: ast.Call, name: object) -> str | None:
        if (
            isinstance(name, str)
            and name.rsplit(".", 1)[-1] == "DomainScheduler"
            and name != "DomainScheduler.from_settings"
        ):
            return "direct DomainScheduler build"
        return None

    offenders = _app_call_offenders("crawlutron/transport/scheduler.py", direct_build)
    assert not offenders, "DomainScheduler constructed outside its home: " + ", ".join(offenders)


def test_global_concurrency_not_getattr_redefaulted():
    """#142: the scheduler's global limit is read from its settings home --
    a ``getattr(settings, "global_concurrency", ...)`` re-defaults the knob
    outside from_settings' never-re-defaulted contract."""

    def getattr_redefault(node: ast.Call, _name: object) -> str | None:
        args = node.args
        if len(args) >= 3 and isinstance(args[1], ast.Constant) and args[1].value == "global_concurrency":
            return "global_concurrency re-defaulted via getattr"
        return None

    offenders = _app_call_offenders("crawlutron/transport/scheduler.py", getattr_redefault)
    assert not offenders, "global_concurrency re-defaulted via getattr: " + ", ".join(offenders)
