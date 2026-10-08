"""How bytes become text: the extraction layer.

Every strategy's bytes arrive here and leave as a consistent ``(text, markdown)``
pair, whatever was fetched: an HTML page loses its boilerplate, a PDF or DOCX
becomes Markdown, plain text passes through. The caller never learns which.

Three modules carry the work. ``content`` is the pipeline every strategy funnels
through, and it also owns the structured diagnostics that come back with the page
-- discovered links, frames, forms, JS endpoints, pagination. ``classify`` turns
extracted text into a taxonomy verdict, with the signature that produced it as
evidence. ``document_text`` normalizes the text a document scraper produced.

Shaping the result for a caller is here too rather than in the API layer:
``llm_format`` reads LLM-oriented metadata out of HTML, and ``output_formats``
serializes a finished result set to the format the caller asked for.

What the bytes were, and whether they were allowed at all, is not this layer's
question: fetching is ``app.transport``, permission is ``app.admission``, and
which strategies to try is ``app.orchestration``.
"""

from __future__ import annotations
