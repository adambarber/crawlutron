"""Browser renderer storage_state path validation (allowlist + traversal)."""

import tempfile

from app.crawl.strategies.browser import BrowserRenderer
from app.settings import CrawlerSettings


class TestStorageStatePathValidation:
    def test_storage_state_rejects_path_under_tmp_when_settings_present(self):
        """When settings are provided, only ``cache_dir`` is an allowed base."""
        settings = CrawlerSettings(cache_dir="/app/cache")
        r = BrowserRenderer(settings=settings)
        # Neither the platform temp dir nor anything else outside cache_dir.
        assert r._validate_storage_state_path(f"{tempfile.gettempdir()}/some-storage.json") is None
        assert r._validate_storage_state_path("/etc/some-storage.json") is None
        assert r._validate_storage_state_path("/app/cache/some-storage.json") is not None

    def test_storage_state_allows_platform_temp_dir_when_no_settings(self):
        """With no settings the allowlist is the platform temp dir, and only that.

        This fallback branch is what makes local-mode dev usage work, and it keys
        off ``tempfile.gettempdir()`` -- so the test has to build its path from
        the same call. A hardcoded ``/tmp`` asserts against Linux's temp dir
        while the code checks this platform's, which is how the branch went
        uncovered: the two agreed on one OS and disagreed on the other.
        """
        r = BrowserRenderer()
        allowed = f"{tempfile.gettempdir()}/some-storage.json"
        assert r._validate_storage_state_path(allowed) is not None
        # An allowlist, not a pass-everything fallback.
        assert r._validate_storage_state_path("/app/cache/some-storage.json") is None

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
        """No-settings callers (local dev) still allow the platform temp dir.

        ``tempfile.gettempdir()``, not a literal ``/tmp``: on macOS the platform
        temp dir is a per-user ``/var/folders/...`` path, and on Linux ``/tmp`` is
        world-readable, which is precisely the exposure the validator's own
        docstring says it refuses. Hardcoding ``/tmp`` here would have pinned the
        test to a platform assumption the production code never made.
        """
        import tempfile

        r = BrowserRenderer()
        assert r._cache_dir is None
        # The platform temp dir is allowed in local-dev mode.
        assert r._validate_storage_state_path(f"{tempfile.gettempdir()}/storage.json") is not None
        # But not /elsewhere
        assert r._validate_storage_state_path("/elsewhere/x.json") is None

    def test_browser_renderer_stores_cache_dir(self):
        """The cache_dir set on settings is accessible for storage_state validation."""

        settings = CrawlerSettings(cache_dir="/var/lib/crawlutron/cache")
        r = BrowserRenderer(settings=settings)
        assert r._cache_dir == "/var/lib/crawlutron/cache"
