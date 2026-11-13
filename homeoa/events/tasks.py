"""Procrastinate task backing job-mode event handlers (autodiscovered by procrastinate)."""

from procrastinate.contrib.django import app


@app.task(name="homeoa.events.run_event_handler", queue="events")
def run_event_handler(handler: str, event: str, payload: dict) -> None:
    from homeoa.audit import audit_context
    from homeoa.events import run_job_handler

    with audit_context(label=f"job:{handler}"):
        run_job_handler(handler, event, payload)
