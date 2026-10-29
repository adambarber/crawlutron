"""The ``ssrf_guard_required`` fixture: prove an SSRF test opted out of loopback."""

import os

import pytest


@pytest.fixture
def ssrf_guard_required(monkeypatch):
    """Mark a test that exercises an SSRF guard and assert it opted out of
    the test default CRAWLUTRON_ALLOW_PRIVATE_TARGETS=1; otherwise the guard
    short-circuits before the real path runs."""
    called: list[str] = []
    from crawlutron.transport import pinned_transport as pinned_mod

    def _track(name: str, fn):
        def wrapper(*args, **kwargs):
            called.append(name)
            # That is the silent false-positive this fixture exists to prevent.
            if os.environ.get("CRAWLUTRON_ALLOW_PRIVATE_TARGETS") != "0":
                pytest.fail(
                    f"{name} was called but CRAWLUTRON_ALLOW_PRIVATE_TARGETS "
                    f"is still enabled (value={os.environ.get('CRAWLUTRON_ALLOW_PRIVATE_TARGETS')!r}). "
                    f"This test exercises the SSRF guard and must opt out via "
                    f"_assertions.block_private_targets(monkeypatch) or "
                    f"monkeypatch.setenv('CRAWLUTRON_ALLOW_PRIVATE_TARGETS', '0') "
                    f"BEFORE invoking the SSRF helper."
                )
            return fn(*args, **kwargs)

        return wrapper

    monkeypatch.setattr(
        pinned_mod,
        "check_ssrf_protection",
        _track("check_ssrf_protection", pinned_mod.check_ssrf_protection),
    )
    monkeypatch.setattr(
        pinned_mod, "resolve_and_pin_host", _track("resolve_and_pin_host", pinned_mod.resolve_and_pin_host)
    )
    yield
    # Sanity check in setup phase (not teardown) so the failure surfaces as a clean FAILED test, not an ERROR
    # from the fixture finalizer.
    _ = called  # silence unused warning if a future check is added
