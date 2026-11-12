"""The refusal surface: typed errors a caller can match.

The door is ``async with gate.admit(want)`` -- a live :class:`Granted` or a raise of
:class:`AdmissionRefused`. Parking (RETRY_PRESSURE) is internal and never surfaces.
"""

from __future__ import annotations

from admission.values import IdleReason

__all__ = ["AdmissionRefused", "NestedAdmitError", "WrongGateError"]


class AdmissionRefused(Exception):
    """A terminal refusal raised from ``__aenter__``; carries a public reason."""

    def __init__(self, reason: IdleReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


class WrongGateError(Exception):
    """A lease released to a gate that did not grant it."""

    def __init__(self, *, lease_owner: object, gate: object) -> None:
        super().__init__("lease released to the wrong gate")
        self.lease_owner = lease_owner
        self.gate = gate


class NestedAdmitError(Exception):
    """An admit executed while another of the same gate's leases is live in this task."""

    def __init__(self) -> None:
        super().__init__("nested admit inside a live lease body is a caller error (v1 prohibition)")
