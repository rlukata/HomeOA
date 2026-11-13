from django.core.management.base import BaseCommand, CommandError

from homeoa.accounts.management.e2e_guard import require_test_mode
from homeoa.core import status
from homeoa.core.models import Association


class Command(BaseCommand):
    help = "Test only: resume (on) or pause (off) resident access."

    def add_arguments(self, parser):
        parser.add_argument("state", choices=["on", "off"])

    def handle(self, *args, state, **options):
        require_test_mode()
        associations = list(Association.objects.all())
        if state == "on":
            not_live = [a.name for a in associations if not status.is_live(a)]
            if not_live:
                raise CommandError(
                    "Resident access opens only once an association is live; not live yet: "
                    + ", ".join(not_live)
                )
        for association in associations:
            if state == "on":
                status.resume_residents(association, reason="e2e_residents")
            else:
                status.pause_residents(association, reason="e2e_residents")
        self.stdout.write(state)
