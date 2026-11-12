"""build_stores: the embedder-facing store bootstrap factory and its container.

The factory builds the six bound stores and wires the audit sink in one call;
create_app routes through it, so a second assembly site cannot appear.
"""

import dataclasses
import pathlib

import pytest

from crawlutron.config.settings import CrawlerSettings
from crawlutron.service.store_bootstrap import Stores, build_stores

_SIX_ATTRS = (
    "job_store",
    "result_store",
    "spill_store",
    "watch_store",
    "caller_key_store",
    "audit_store",
)


def _settings_with_cache_dir(tmp_path) -> CrawlerSettings:
    """Settings whose cache_dir points at a scratch dir (never ./cache)."""
    return dataclasses.replace(CrawlerSettings.from_mapping(), cache_dir=str(tmp_path))


def _close_stores(stores) -> None:
    """Close the six handles a build opened, mirroring the shutdown door."""
    for attr in _SIX_ATTRS:
        store = getattr(stores, attr)
        if store is not None:
            store.close()


def _assert_all_stores_at(stores, expected: pathlib.Path) -> None:
    """Every store handle reports the same expected database file."""
    target = expected.resolve()
    for attr in _SIX_ATTRS:
        store = getattr(stores, attr)
        got = pathlib.Path(store.db_path).resolve()
        assert got == target, f"{attr} opened {got}, not {target}"


@pytest.fixture
def closed_build():
    """Build stores and close every handle the test opened (leak-free)."""
    built: list[Stores] = []

    def _build(settings, database_path=None) -> Stores:
        stores = build_stores(settings, database_path)
        built.append(stores)
        return stores

    yield _build
    for stores in built:
        _close_stores(stores)


class TestBuildStores:
    def test_build_stores_returns_bound_stores_and_attaches_audit(self, closed_build, tmp_path):
        """The factory returns the six store handles and wires the audit sink.

        The audit assertion runs through the public doors: minting a key through
        the caller-key store records a row the audit store lists back.
        """
        stores = closed_build(_settings_with_cache_dir(tmp_path))

        for attr in _SIX_ATTRS:
            assert getattr(stores, attr) is not None, attr

        stores.caller_key_store.issue_key("wiring probe")
        events = [e.action for e in stores.audit_store.list_events()]
        assert "key_mint" in events, "the caller-key store never reached the audit sink"

    def test_build_stores_derives_path_from_settings_by_default(self, closed_build, tmp_path):
        """Without a path argument the factory derives cache_dir/crawler.db."""
        stores = closed_build(_settings_with_cache_dir(tmp_path))
        _assert_all_stores_at(stores, pathlib.Path(tmp_path, "crawler.db"))

    def test_explicit_database_path_overrides_settings(self, closed_build, tmp_path):
        """An explicit path wins over the settings-derived default."""
        settings = _settings_with_cache_dir(tmp_path / "ignored")
        explicit = tmp_path / "chosen" / "store.db"
        stores = closed_build(settings, database_path=str(explicit))
        _assert_all_stores_at(stores, explicit)

    def test_build_twice_yields_independent_store_sets(self, closed_build, tmp_path):
        """Two builds are independent: no hidden singleton is reused."""
        first = closed_build(_settings_with_cache_dir(tmp_path))
        second = closed_build(_settings_with_cache_dir(tmp_path))
        for attr in _SIX_ATTRS:
            assert getattr(first, attr) is not getattr(second, attr), attr

    def test_container_is_frozen_and_field_strict(self, closed_build, tmp_path):
        """The container refuses unknown attributes (frozen)."""
        stores = closed_build(_settings_with_cache_dir(tmp_path))
        with pytest.raises(dataclasses.FrozenInstanceError):
            stores.not_a_store = object()

    def test_container_requires_all_six_fields(self, tmp_path):
        """Every field is required: a partial six-store set cannot be built, so
        the one-missing-store boundary is refused at the type level."""
        fields = dataclasses.fields(Stores)
        assert [f.name for f in fields] == list(_SIX_ATTRS)
        assert all(f.default is dataclasses.MISSING for f in fields), [
            f.name for f in fields if f.default is not dataclasses.MISSING
        ]

    def test_build_stores_creates_a_missing_cache_tree(self, closed_build, tmp_path):
        """A settings whose cache_dir has no parent: the factory (like the store
        it wraps) creates the tree rather than failing on a missing directory."""
        deep = tmp_path / "a" / "b" / "c"
        settings = dataclasses.replace(_settings_with_cache_dir(tmp_path), cache_dir=str(deep))
        stores = closed_build(settings)
        assert stores.job_store.db_path.parent.exists()


class TestCreateAppConsumesBuildStores:
    def test_create_app_routes_through_the_factory(self, tmp_path, monkeypatch):
        """create_app must call build_stores, not rebuild stores inline.

        A second assembly site is the bypass: patch the factory seam and the app
        build must route through it exactly once with the derived path.
        """
        import crawlutron.main as main_mod

        calls: list[CrawlerSettings] = []
        real_build = build_stores

        def _spy(settings, database_path=None):
            calls.append(settings)
            return real_build(settings, database_path)

        monkeypatch.setattr(main_mod, "build_stores", _spy)
        settings = _settings_with_cache_dir(tmp_path)
        app = main_mod.create_app(settings)
        try:
            assert len(calls) == 1, "create_app must build stores exactly once"
            assert calls[0] is settings, "create_app built stores against the wrong settings"
            derived = pathlib.Path(app.state.job_store.db_path).resolve()
            assert derived == pathlib.Path(tmp_path, "crawler.db").resolve()
        finally:
            _close_stores(app.state)
