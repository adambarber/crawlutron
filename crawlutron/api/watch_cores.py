"""Narrow re-export shim: the module moved to crawlutron/service (#849)."""

from crawlutron.service.watch_cores import (
    delete_watch_core as delete_watch_core,
)
from crawlutron.service.watch_cores import (
    get_watch_core as get_watch_core,
)
from crawlutron.service.watch_cores import (
    list_watches_core as list_watches_core,
)
from crawlutron.service.watch_cores import (
    register_watch_core as register_watch_core,
)
