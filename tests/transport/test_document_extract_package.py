"""A document extracted through transport/document.py gets its text from the package."""

from crawlutron.transport.document import default_document_text_extractor
from tests._policy_factories import canonical


async def test_document_extract_produces_text_from_package(base_url):
    url = f"{base_url}/sample_table.pdf"
    text = default_document_text_extractor(canonical(url))
    assert text, "the package must produce text for a real PDF"
    assert "Alice" in text
