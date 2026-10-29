"""The ``override_settings`` fixture: env plus overrides becomes settings."""

import os

import pytest


@pytest.fixture
def override_settings():
    """Point ``app.state.crawler_settings`` at a fresh instance for a test.

    The suite's only spelling of "env plus overrides becomes settings";
    test files used to carry private module-level copies of the same lines."""
    from crawlutron.config.settings import CrawlerSettings
    from tests._app_factory import service_app

    app = service_app()

    original = app.state.crawler_settings

    def _apply(**overrides) -> CrawlerSettings:
        merged = dict(os.environ)
        for key, value in overrides.items():
            merged[f"CRAWLUTRON_{key.upper()}"] = str(value)
        settings = CrawlerSettings.from_mapping(merged)
        app.state.crawler_settings = settings
        return settings

    yield _apply
    app.state.crawler_settings = original
