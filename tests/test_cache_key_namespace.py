"""Two crawls sharing headers but not identity never share a cache entry (#544/S5)."""

from app.models.normalized_url import NormalizedUrl
from app.transport.content_cache_key import build_content_cache_key
from tests._policy_factories import UNRESTRICTED_HOST_POLICY


def _key(namespace: str) -> str:
    return build_content_cache_key(
        NormalizedUrl("https://example.com/page", policy=UNRESTRICTED_HOST_POLICY),
        "http",
        {},
        None,
        None,
        None,
        cookie_namespace=namespace,
    )


def test_different_namespaces_yield_different_keys() -> None:
    assert _key("tenant-a") != _key("tenant-b")
    assert _key("tenant-a") == _key("tenant-a")


def test_default_namespace_keys_stay_stable() -> None:
    assert _key("") == _key("")
