"""Events published by the jobs app."""

import datetime as dt
from dataclasses import dataclass

from homeoa.events import Event


@dataclass(frozen=True, kw_only=True)
class JobRunFailed(Event):
    """A periodic job run failed and reached HOMEOA_SCHEDULER_MAX_ATTEMPTS (to-do)."""

    name = "jobs.run_failed"
    job_name: str
    as_of: dt.date
    attempts: int
    run_id: int
