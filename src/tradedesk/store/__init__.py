"""Local SQLite history for IV surfaces, funding, basis and periodic snapshots.

The store is READ+WRITE used by the collector and READ-only by the history tools.
Writes are small and infrequent (5–60 min cadence), so aiosqlite with WAL is enough.
"""

from .db import Store
from .schema import SCHEMA_VERSION

__all__ = ["Store", "SCHEMA_VERSION"]
