"""Browser renderer storage_state path validation (allowlist + traversal)."""

import tempfile

from app.config.settings import CrawlerSettings
from app.transport.browser import BrowserRenderer


class TestStorageStatePathValidation:
    def test_storage_state_rejects_path_under_tmp_when_settings_present(self):
        """When settings are provided, only ``cache_dir`` is an allowed base."""
        settings = CrawlerSettings(cache_dir="/app/cache")
        r = BrowserRenderer(settings=settings)
        # Neither the platform temp dir nor anything else outside cache_dir.
        assert r.driver.validate_storage_state_path(f"{tempfile.gettempdir()}/some-storage.json") is None
        assert r.driver.validate_storage_state_path("/etc/some-storage.json") is None
        assert r.driver.validate_storage_state_path("/app/cache/some-storage.json") is not None

    def test_no_cache_dir_means_no_allowed_storage_state_path(self):
        """Fail closed: with no ``cache_dir`` there is no allowlist, so nothing passes.

        The branch this replaces allowed anything under ``tempfile.gettempdir()``,
        and the sibling test that covered it recorded the exposure in its own
        docstring: on Linux ``/tmp`` is world-readable, which is precisely what
        the validator's docstring said it refused. A ``storage_state`` is cookies
        and tokens, so a directory any local user can write is a directory any
        local user can seed -- Playwright would load the file the attacker
        planted instead of one an operator chose.

        Failing closed costs a local dev who passes a path with no configured
        ``cache_dir`` a diagnostic and an explicit config line. The alternative
        costs a deployment its session, silently.
        """
        r = BrowserRenderer()
        assert r.driver.cache_dir is None
        assert r.driver.validate_storage_state_path(f"{tempfile.gettempdir()}/storage.json") is None
        assert r.driver.validate_storage_state_path(f"{tempfile.gettempdir()}/../etc/storage.json") is None
        assert r.driver.validate_storage_state_path("/app/cache/some-storage.json") is None
        assert r.driver.validate_storage_state_path("relative/storage.json") is None

    def test_refusing_without_a_cache_dir_names_the_setting_to_configure(self, caplog):
        """A refused path must say WHY it was refused, not just return None.

        Returning None for every path is indistinguishable from "that path is not
        allowed" -- the operator gets a render that quietly drops their session
        with nothing in the logs. The configured-``cache_dir`` rejections are
        silent by design, since there the allowlist exists and the answer is
        simply no; the no-``cache_dir`` case is different in kind, because no
        allowlist was ever configured and that is the actionable fact.
        """
        import logging

        r = BrowserRenderer()
        # The refusal is raised by the driver's validator, so the record is emitted under that module's name.
        with caplog.at_level(logging.WARNING, logger="app.transport.browser_driver"):
            assert r.driver.validate_storage_state_path(f"{tempfile.gettempdir()}/storage.json") is None

        messages = [record.getMessage() for record in caplog.records]
        assert any("cache_dir" in message for message in messages), (
            f"the refusal did not name the setting to configure: {messages}"
        )
        assert [record.name for record in caplog.records] == ["app.transport.browser_driver"], (
            f"the refusal moved module; the logger pinned above is now the wrong one: "
            f"{[record.name for record in caplog.records]}"
        )

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
            assert r.driver.validate_storage_state_path(key) is None, (
                f"{key!r} was accepted as a storage_state, which Playwright would read as a "
                f"file named {key!r} in the process working directory"
            )

    def test_storage_state_rejects_path_traversal(self):

        settings = CrawlerSettings(cache_dir="/app/cache")
        r = BrowserRenderer(settings=settings)
        assert r.driver.validate_storage_state_path("/app/elsewhere/x.json") is None

    def test_browser_renderer_stores_cache_dir(self):
        """The cache_dir set on settings is accessible for storage_state validation."""

        settings = CrawlerSettings(cache_dir="/var/lib/crawlutron/cache")
        r = BrowserRenderer(settings=settings)
        assert r.driver.cache_dir == "/var/lib/crawlutron/cache"
