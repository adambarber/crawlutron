"""Scheduler-internal exceptions that double as classification inputs.

Homed here next to the classifier (not ``scheduler``) so ``classify_error_code``
can key off the type without a cycle -- classification lives at the chokepoint.
"""


class CircuitBreakerOpen(RuntimeError):
    """Raised when a domain's breaker is open and the job is skipped; the classifier
    tells it apart from a network error (capacity, not a transport outcome). The message
    names the domain and remaining cooldown seconds -- never a bare counter value (#347).
    """
