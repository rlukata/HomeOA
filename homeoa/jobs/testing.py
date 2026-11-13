"""Test helpers: run deferred Procrastinate jobs synchronously.

from homeoa.jobs.testing import capture_jobs

with capture_jobs() as queue:
    publish(SomethingHappened(association_id=a.pk))
    assert queue.pending_names() == ["homeoa.events.run_event_handler"]
    queue.run_all()   # runs in this thread, inside the test transaction
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from procrastinate import testing
from procrastinate.contrib.django import app


class CapturedQueue:
    def __init__(self, connector: testing.InMemoryConnector, app_):
        self.connector = connector
        self.app = app_
        self._done: set[int] = set()

    def pending(self) -> list[dict]:
        return [j for jid, j in sorted(self.connector.jobs.items()) if jid not in self._done]

    def pending_names(self) -> list[str]:
        return [j["task_name"] for j in self.pending()]

    def run_all(self) -> int:
        """Run pending jobs (including ones deferred while running) in order; return count."""
        count = 0
        while jobs := self.pending():
            for job in jobs:
                self._done.add(job["id"])
                self.app.tasks[job["task_name"]].func(**job["args"])
                count += 1
        return count


@contextmanager
def capture_jobs() -> Iterator[CapturedQueue]:
    connector = testing.InMemoryConnector()
    with app.replace_connector(connector) as replaced:
        yield CapturedQueue(connector, replaced)
