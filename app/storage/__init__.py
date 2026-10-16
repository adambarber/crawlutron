"""How things persist: the storage layer.

``sqlite_store`` owns the connection/lock/journal, ``coordination_store`` is the base, and
``migrations`` is the only place a schema may change; store handles are minted in ``ids``.
"""

from __future__ import annotations
