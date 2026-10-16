"""Scheduler-internal exceptions that double as classification inputs.

Homed here next to the classifier (not ``scheduler``) so ``classify_error_code``
can key off the type without a cycle -- classification lives at the chokepoint.
"""


class CircuitBreakerOpen(RuntimeError):
    """Raised when a domain's breaker is open and the job is skipped; the classifier
    distinguishes it from a network error (a scheduler capacity decision, not a transport
    outcome), keeping redaction/taxonomy checks in the single envelope chokepoint.
    """
