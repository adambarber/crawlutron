"""Scheduler-internal exceptions that double as classification inputs.

These live here, alongside the error classifier, rather than in ``scheduler``:
``classify_error_code`` keys the taxonomy off the exception type, so the
exception must be importable from the classifier without a cycle
(``scheduler`` does not import ``result_helpers``). Keeping the type next to
the function that reads it is what makes the classification a property of the
chokepoint instead of something each caller has to restate.
"""


class _CircuitBreakerOpen(RuntimeError):
    """Raised internally when a domain's breaker is open and the job is skipped.

    The classifier maps this to ``circuit_breaker_open`` so the failure is
    distinguishable from an ordinary network error: it is a capacity decision
    by the scheduler, not a transport outcome, and callers (retry policy,
    dashboards) key off the distinction. Carrying it as an exception (rather
    than a pre-built result) keeps the redaction and taxonomy check inside the
    single envelope chokepoint.
    """
