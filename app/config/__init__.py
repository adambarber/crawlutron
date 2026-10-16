"""What the operator configured, and the one way it becomes a value.

``env`` parses every ``CRAWLUTRON_*`` read (type/range/failure in one place);
``settings`` is the assembled result. Ranges live in ``app.safety``, not here.
"""

from __future__ import annotations
