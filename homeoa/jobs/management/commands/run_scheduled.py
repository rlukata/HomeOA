import datetime as dt

from django.core.management.base import BaseCommand, CommandError

from homeoa.core.models import Association
from homeoa.jobs.scheduler import periodic_jobs, run_due


class Command(BaseCommand):
    help = (
        "Run every periodic job due on the given date, synchronously. Only that date runs:"
        " missed dates are not caught up (the worker does that)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--as-of", required=True, help="Date as YYYY-MM-DD.")
        parser.add_argument("--association", type=int, help="Only this association id.")
        parser.add_argument("--job", action="append", help="Only this job name (repeatable).")
        parser.add_argument("--rerun", action="store_true", help="Repeat runs that succeeded.")

    def handle(self, *args, **options):
        try:
            as_of = dt.date.fromisoformat(options["as_of"])
        except ValueError as exc:
            raise CommandError("--as-of must be YYYY-MM-DD.") from exc
        association = None
        if options["association"] is not None:
            association = Association.objects.filter(pk=options["association"]).first()
            if association is None:
                raise CommandError("No such association.")
        names = options["job"]
        if names:
            unknown = set(names) - set(periodic_jobs())
            if unknown:
                raise CommandError(f"Unknown job(s): {', '.join(sorted(unknown))}")
        runs = run_due(as_of=as_of, association=association, names=names, rerun=options["rerun"])
        failed = 0
        for run in runs:
            self.stdout.write(f"{run.association_id} {run.job_name} {run.as_of} {run.status}")
            failed += run.status == run.Status.FAILED
        self.stdout.write(f"{len(runs)} run(s), {failed} failed.")
        if failed:
            raise CommandError(f"{failed} job run(s) failed.")
