"""Count of provider rows / fields a collector job dropped as malformed, per scheduler run (item 307; no I/O).

Job code calls :func:`note_bad_rows` next to its "dropped" / "stored as NULL" log line; the scheduler opens a fresh
count per run with :func:`count_bad_rows` and stores it on ``JobMetrics`` (``system_health`` -> ``collector[]``). The
count lives in a :class:`contextvars.ContextVar`, so job signatures stay as they are: each job runs in its own task
(its own context), so concurrent jobs never mix counts, and sub-tasks a job spawns (``asyncio.gather``) copy the
context and add to the same mutable count. Outside a counted run (an MCP tool calling a shared parser) it is a no-op.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass
class BadRows:
    n: int = 0


_CURRENT: ContextVar[BadRows | None] = ContextVar("tradedesk_collector_bad_rows", default=None)


def note_bad_rows(n: int) -> None:
    """Add ``n`` dropped (or NULLed) provider rows / fields to the current run's count; no-op outside a run."""
    count = _CURRENT.get()
    if count is not None and n > 0:
        count.n += int(n)


@contextmanager
def count_bad_rows() -> Iterator[BadRows]:
    """A fresh count for the code run inside the block (the current task and the tasks it spawns)."""
    count = BadRows()
    token = _CURRENT.set(count)
    try:
        yield count
    finally:
        _CURRENT.reset(token)
