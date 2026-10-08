"""May this page be fetched at all: the admission layer.

Every URL a crawl touches passes through here before a strategy is asked to fetch
it. The question is not whether the request succeeds -- that is transport's -- but
whether it is permitted, in scope, and worth the budget: the policy's allow-list
and SSRF verdict, the scope patterns, the robots decision, the caller credentials
that must never cross a host boundary, and the per-run bounds.

`policy` is the read edge between a caller's policy and the validated model:
`read_policy` is the one place a stored policy becomes a usable one, so a job
cannot come back with bounds nobody checked. `payload_helpers` checks the URLs a
request carries before anything is scheduled. `render_options` resolves request-side
options against the policy, which is why it belongs here rather than in the API
layer that reads the options.

Two admission questions are separate on purpose. A whole-site run admits pages as
it discovers them (`admission`, with `scope_match` beside it); a recursive run
carries its own admission state and cap predicate (`recursive_admission`), which is
why the recursive run's decisions live in their own module rather than as a flag on
the other one.

What an admitted URL then costs to fetch is `app.transport`; what a crawl does with
the results is `app.orchestration`.
"""

from __future__ import annotations
