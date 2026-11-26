"""Admin-app store locality (#1490): the explicit settings mapping keeps the suite scratch dir."""

from __future__ import annotations

import os
from pathlib import Path

from crawlutron.config.settings import CrawlerSettings
from crawlutron.service.store_shutdown import close_app_stores
from crawlutron_rest.main import create_app


async def test_admin_client_store_is_in_suite_scratch_dir():
    """#1490: the explicit settings mapping must carry the suite scratch cache dir."""
    settings = CrawlerSettings.from_mapping({"CRAWLUTRON_CACHE_DIR": os.environ["CRAWLUTRON_CACHE_DIR"]})
    app = create_app(settings)
    try:
        db_path = app.state.caller_key_store.db_path.resolve()
        scratch = Path(os.environ["CRAWLUTRON_CACHE_DIR"]).resolve()
        assert db_path.is_relative_to(scratch), f"{db_path} must live under {scratch}"
        assert db_path != Path("./cache/crawler.db").resolve(), "all workers share ./cache"
    finally:
        await close_app_stores(app)
