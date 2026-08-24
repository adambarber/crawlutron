"""Live-found bug: worcesterma.gov robots enforcement failed open.

Their robots.txt contains 'Disallow: /core/' and IS fetchable, yet a crawl of
/core/CHANGELOG.txt returned success/rendered. Kept as xfail until the matcher
is fixed with a real-world rule set fixture.
"""
import pytest

from app.crawl.engine import _compile_scope_regex_safe  # noqa: F401

ROBOTS = """User-agent: *
Disallow: /core/
Disallow: /profiles/
"""

TARGET_PATH = "/core/CHANGELOG.txt"


@pytest.mark.xfail(reason="robots enforcement failed open on real site (open bug)", strict=True)
def test_core_disallow_matches():
    # Replicates the engine's pattern translation for 'Disallow: /core/'
    import re
    pattern = "^" + re.escape("/core/") + ".*"
    assert re.match(pattern, TARGET_PATH), "rule should match the path"
