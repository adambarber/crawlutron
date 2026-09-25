"""Browser renderer storage_state path validation (allowlist + traversal)."""

from app.crawl.strategies.browser import BrowserRenderer
from app.settings import CrawlerSettings


class TestStorageStatePathValidation:
    def test_storage_state_rejects_path_under_tmp_when_settings_present(self):
        """When settings are provided, ``/tmp`` is NOT an allowed base."""

        settings = CrawlerSettings(cache_dir="/app/cache")
        r = BrowserRenderer(settings=settings)
        # /tmp is allowed only when no settings were supplied
        assert r._validate_storage_state_path("/tmp/some-storage.json") is None
        # /app/cache is allowed
        assert r._validate_storage_state_path("/app/cache/some-storage.json") is not None

    def test_storage_state_rejects_bare_names_that_are_not_paths(self):
        """A string is a FILE PATH to Playwright, not the name of a session.

        ``storage_state`` is ``Union[str, pathlib.Path] | Dict`` -- "can be a
        path to a file or a dictionary". There is no named-session concept, so
        passing "default" / "session" / "persistent" asks the browser to read a
        file of that name relative to the process CWD. The validator used to
        return those three strings unchanged, so the one chokepoint whose
        documented job is stopping a caller from reading a path it should not
        was itself handing out an unvalidated relative path -- and either
        failing the render on a missing file or, if such a file existed,
        loading its cookies into the context.
        """
        settings = CrawlerSettings(cache_dir="/app/cache")
        r = BrowserRenderer(settings=settings)
        for key in ("default", "session", "persistent"):
            assert r._validate_storage_state_path(key) is None, (
                f"{key!r} was accepted as a storage_state, which Playwright would read as a "
                f"file named {key!r} in the process working directory"
            )

    def test_storage_state_rejects_path_traversal(self):

        settings = CrawlerSettings(cache_dir="/app/cache")
        r = BrowserRenderer(settings=settings)
        assert r._validate_storage_state_path("/app/elsewhere/x.json") is None

    def test_storage_state_fallback_to_tmp_only_when_no_settings(self):
        """No-settings callers (local dev) still allow /tmp."""
        r = BrowserRenderer()
        assert r._cache_dir is None
        # /tmp is allowed in local-dev mode
        assert r._validate_storage_state_path("/tmp/storage.json") is not None
        # But not /elsewhere
        assert r._validate_storage_state_path("/elsewhere/x.json") is None

    def test_browser_renderer_stores_cache_dir(self):
        """The cache_dir set on settings is accessible for storage_state validation."""

        settings = CrawlerSettings(cache_dir="/var/lib/browser-crawler/cache")
        r = BrowserRenderer(settings=settings)
        assert r._cache_dir == "/var/lib/browser-crawler/cache"
