# crawlutron-admit

Resource-adaptive paced admission for dispatch loops. One typed gate owns ramp,
per-lane floors, capacity, leases, and liveness; callers own queueing and work.

The door is `async with gate.admit(want) as granted:` — a live grant with a lease,
or a typed `AdmissionRefused`. Time and wakeups cross through injected `Clock` and
`Signal` seams; cgroup memory/CPU sensing is included.
