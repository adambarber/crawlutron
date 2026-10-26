"""Header NAMES carry the same injectable-bytes law as values (#544/S3)."""

import pytest
from pydantic import ValidationError

from app.models.crawl_policy import CrawlPolicy


@pytest.mark.parametrize("name", ["X-Evil\r\nX-Injected: yes", "X-Bad\n", "X-Nul\x00"])
def test_header_name_with_wire_bytes_is_refused(name: str) -> None:
    with pytest.raises(ValidationError, match="smuggle"):
        CrawlPolicy(headers={name: "value"})


def test_clean_names_still_admit() -> None:
    policy = CrawlPolicy(headers={"Accept": "text/html", "X-Custom-Header": "v"})
    assert policy.headers["Accept"] == "text/html"
