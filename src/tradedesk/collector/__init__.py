"""Background collector: periodic jobs that persist history into the Store.

Enabled per-process via ``TRADEDESK_COLLECTOR_ENABLED=1``; otherwise the
scheduler never starts and the server runs purely as an on-demand API.
"""

from .scheduler import JobMetrics, PeriodicJob, Scheduler, build_default_scheduler

__all__ = ["Scheduler", "PeriodicJob", "JobMetrics", "build_default_scheduler"]
