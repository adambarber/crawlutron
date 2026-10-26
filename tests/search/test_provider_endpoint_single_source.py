"""No provider endpoint URL literal may appear in more than one of the three
provider modules: ``providers.py`` is the single home (the #386 registry)."""

import re
from pathlib import Path

import pytest

from app.search.providers import PROVIDER_ENDPOINTS

_MODULES = [
    Path("app/search/providers.py"),
    Path("app/search/providers_api.py"),
    Path("app/search/providers_html.py"),
]

# Anything that looks like a URL literal, including the scheme-relative DDG
# redirect prefix.
_URL_RE = re.compile(r'"(https://[^"]+|//[^"]+)"')


def _url_literals(path: Path) -> set[str]:
    return set(_URL_RE.findall(path.read_text()))


def test_registry_matches_declared_urls() -> None:
    root = Path(__file__).resolve().parents[2]
    for url in PROVIDER_ENDPOINTS:
        assert url in _url_literals(root / "app/search/providers.py"), url


@pytest.mark.parametrize("module", _MODULES[1:], ids=lambda p: p.name)
def test_no_endpoint_literal_outside_providers(module: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    leaked = {u for u in _url_literals(root / module) if u in PROVIDER_ENDPOINTS}
    assert not leaked, f"{module} restates registry endpoints: {sorted(leaked)}"
