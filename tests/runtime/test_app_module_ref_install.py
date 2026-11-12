"""The app-module provider installs exactly once; reinstall goes through the reset door."""

import pytest

from crawlutron.runtime import app_module_ref as ref
from crawlutron.runtime.app_module_ref import AppModuleLike, module_app, set_module_app_provider


class _SentinelApp:
    """A minimal AppModuleLike stand-in carrying a distinguishable marker."""

    def __init__(self, marker: str) -> None:
        self._marker = marker

    @property
    def state(self) -> object:
        return self._marker


def _sentinel_provider(marker: str):
    def _provider() -> AppModuleLike:
        return _SentinelApp(marker)

    return _provider


@pytest.fixture(autouse=True)
def _clean_provider():
    """Snapshot and restore the module's provider wiring around each test."""
    saved = ref.provider_wiring_snapshot()
    try:
        ref.reset_module_app_provider()
    except RuntimeError:
        pass
    yield
    ref.restore_provider_wiring(saved)


def test_second_install_refuses_naming_both_sites():
    set_module_app_provider(_sentinel_provider("first"))
    first_site = f"{__file__}"
    with pytest.raises(RuntimeError) as err:
        set_module_app_provider(_sentinel_provider("second"))
    message = str(err.value)
    assert "already installed" in message
    assert first_site.split("/")[-1] in message, message


def test_reset_then_install_is_the_sanctioned_door():
    set_module_app_provider(_sentinel_provider("first"))
    ref.reset_module_app_provider()
    set_module_app_provider(_sentinel_provider("second"))
    assert isinstance(module_app(), _SentinelApp)


def test_reset_with_nothing_installed_refuses_loudly():
    with pytest.raises(RuntimeError, match="not installed"):
        ref.reset_module_app_provider()


def test_aliased_setter_binding_cannot_route_around_the_guard():
    alias = set_module_app_provider
    set_module_app_provider(_sentinel_provider("first"))
    with pytest.raises(RuntimeError, match="already installed"):
        alias(_sentinel_provider("second"))


def test_identical_rewiring_from_the_same_site_is_idempotent():
    """A module re-import re-runs the SAME call at the SAME site: same wiring, not a second site."""
    provider = _sentinel_provider("first")
    for _ in range(2):
        set_module_app_provider(provider)  # one call site, twice: the re-import shape
    assert isinstance(module_app(), _SentinelApp)


def test_reload_fresh_callable_at_the_same_site_is_still_idempotent():
    """importlib.reload re-executes main.py: a NEW callable from the SAME site is the
    same wiring (the reload shape xdist exercises), never a second installation site."""
    for i in range(2):
        set_module_app_provider(_sentinel_provider(f"first-{i}"))  # same line, new callable each pass
    assert isinstance(module_app(), _SentinelApp)


def test_module_app_without_a_provider_raises():
    with pytest.raises(RuntimeError, match="not installed"):
        module_app()
