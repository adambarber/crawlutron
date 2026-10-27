"""One scheduler construction home: DomainScheduler.from_settings (#482)."""

from app.config.settings import CrawlerSettings
from app.transport.scheduler import DomainScheduler

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
    from app.admission.admission import JobAdmissionContext
    from app.admission.policy import PolicyTier
    from app.models.crawl_policy import CrawlPolicy

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
