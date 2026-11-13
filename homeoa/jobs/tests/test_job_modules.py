from homeoa.jobs import periodic_jobs
from homeoa.jobs.scheduler import daily_jobs, job_module
from homeoa.plugins import slots

MODULE_JOBS = {
    "arc.deadline_reminders": "arc",
    "arc.deemed_approvals": "arc",
    "maintenance.preventive": "work_orders",
    "community.announcements_due": "announcements",
    "payments.autopay": "online_payments",
}


def test_each_module_job_is_in_the_daily_jobs_slot_under_its_module_and_the_rest_are_core():
    tagged = {c.value: c.module for c in slots.contributions(slots.DAILY_JOBS)}
    for name, job in periodic_jobs().items():
        assert tagged[name] == MODULE_JOBS.get(name)
        assert job_module(name) == job.module == MODULE_JOBS.get(name)
    assert set(MODULE_JOBS) <= set(periodic_jobs())


def test_the_scheduler_reads_every_registered_job_from_the_slot():
    assert [j.name for j in daily_jobs()] == list(periodic_jobs())
