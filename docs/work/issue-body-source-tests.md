`tests/test_body_size_middleware.py` was eight assertions that a substring appeared in `app/api/middleware.py`'s source. `tests/test_app_lifecycle.py` had two more: "every store shares the one database path" (read `app/main.py` looking for constructor tokens) and "the lifespan is a direct contextmanager" (asserted the `@asynccontextmanager` decorator string).

None of them can fail for a reason a reader would recognise, and two cannot fail at all:

- A test that greps the source passes for any spelling that satisfies the grep. Renaming the parameter, inlining the comparison, or extracting the branch all leave it green.
- The lifespan claim is inert. The lifespan writes `_app.state.shutdown_event`, so the `_lifespan(None)` shape it guarded does not merely read differently -- it raises at startup. The decorator string says nothing about that.
- The store-path claim could not see the failure it names. A store handed two paths, or one that resolved or substituted its own file, would echo the constructor argument back and pass.

Three seams were missing, which is why the source reading was the only route:

- `limit_request_body` had no way to vary its cap, so the only lever was a module-global patch.
- `CookieJar` and the metrics object had no public answer to "which storage backs you", so the only route was `_store` / `_counters`.
- The lifespan built the jar and hid it; what the process was using was reachable only via the module global it assigned.

`limit_request_body` now takes `max_bytes`; `CookieJar.store` and `Metrics.counters` are properties; the lifespan publishes the jar on `app.state` beside the shutdown event. The body-cap file is deleted and its real claims are driven through the middleware, including the one a substring cannot express: that the shipped app actually returns 413 on an oversized body.