"""Public vocabulary values for doc contracts; derived, never transcribed."""

from crawlutron.models.crawl_status import CrawlStatus
from crawlutron.models.crawl_strategy import CrawlStrategy
from crawlutron.vocabulary.taxonomy import CrawlClassification

CLASSIFICATION_VALUES = tuple(member.value for member in CrawlClassification)
RESULT_STATUS_VALUES = tuple(member.value for member in CrawlStatus)
STRATEGY_VALUES = tuple(member.value for member in CrawlStrategy)
