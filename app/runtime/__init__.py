"""What this process is doing, and how much room it has.

The service's own accounting, in two halves that share a package because they
answer one operator's question -- "is this deployment healthy, and can it take
more work?":

- ``metrics`` counts the work: crawls, results, bytes, strategies. It is the
  number a deployment is scraped for.
- ``logging_config`` is the one JSON-structured logger every entry point wires the
  same way, so a log line from the API and a log line from the MCP tool have the
  same shape.
- ``resource_pressure`` is the single answer to "how loaded is this box", and it
  asks ``cgroup_sense`` what the container limit is and ``memory_pressure`` what
  the process is using, rather than each of those reading /proc or cgroup files on
  its own schedule.
- ``process_state`` records the pid-level facts the other two cannot see, and owns
  the reset that makes a reloaded lifespan forget them.

These are process-wide services, not layer behaviour, so they are not spread into
the layers that consult them: transport asks about memory before it admits another
body, the scheduler asks about pressure before it starts a job, and health reports
both. Putting any of them inside a layer would make the other consumers depend on
a layer they have no business importing.

What a crawl is ALLOWED to spend is ``app/vocabulary/limits.py``; whether a URL
may be fetched at all is ``app/admission``.
"""

from __future__ import annotations
