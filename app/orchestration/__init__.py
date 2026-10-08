"""What happens next: the orchestration layer.

Everything that decides *what to do* rather than doing it. The engine is the
engine: given a request, it admits nothing itself (that is `app.admission`) and
fetches nothing itself (that is `app.transport`) -- it decides which strategies to
try, in what order, how many times, and what each outcome means for the job.
`strategy_chain` is that decision, and `request_pipeline` is the shape it runs in.

Around it: `recursive` runs a whole-site frontier, `worker` drains one, `scheduler`
decides which queued job runs next and what a failure means for it,
`scheduler_exceptions` holds the exception types that double as classification
inputs, `fetch_helpers` and `robots` answer per-fetch questions, and `doc_pool`
retires the persistent document-extraction workers.

`result_helpers` is this layer's output boundary: it turns an engine outcome into
the result record a caller receives, and classifies the exceptions that arrive
instead. It sits here rather than in `app/extraction` because its input is the
chain's verdict, not a page's bytes.

`robots` and `robots_parse` are here rather than in `app/transport` because robots
is a crawl-policy decision made per fetch, not a wire-format concern: the transport
carries whatever the policy already allowed.

A single-URL render, a batch crawl and a whole-site run differ in which of these
they use, not in what they mean -- which is why they are one layer rather than three
pipelines with three copies of the retry rules.
"""

from __future__ import annotations
