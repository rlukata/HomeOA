"""The periodic SimpleFIN sync (autodiscovered by Procrastinate).

Ticks every 5 minutes; each association syncs once a day at its own random
``Connection.sync_minute``. The random minute is why this is a Procrastinate
periodic task rather than a ``homeoa.jobs.periodic`` job.
"""

import datetime as dt

from procrastinate.contrib.django import app

from . import sync

TASK_NAME = "homeoa_simplefin.sync_due"


@app.periodic(cron="*/5 * * * *", periodic_id=TASK_NAME)
@app.task(name=TASK_NAME, queue="default", queueing_lock=TASK_NAME)
def sync_due(timestamp: int) -> None:
    """Sync every association whose daily SimpleFIN slot has come."""
    sync.sync_due(dt.datetime.fromtimestamp(timestamp, tz=dt.UTC))
