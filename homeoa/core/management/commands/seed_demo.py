"""``manage.py seed_demo [--reset] [--bare]``: load the Cedar Row sandbox association."""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from homeoa.core import demo
from homeoa.core.models import Association


class Command(BaseCommand):
    help = (
        "Load the Cedar Row demo association in sandbox mode (OPS-05). Refuses to touch a "
        "database holding any non-sandbox association. Seeded users: "
        + "; ".join(f"{email}: {what}" for email, what in demo.USERS.items())
        + ". Aug-Sep dues and payments are posted; October is not."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset", action="store_true", help="wipe the sandbox data and seed again"
        )
        parser.add_argument(
            "--bare",
            action="store_true",
            help="only the association and board users (no units, people or ledger)",
        )

    def handle(self, *args, reset, bare, **options):
        try:
            demo.check_safe()
        except demo.SeedRefused as exc:
            raise CommandError(str(exc)) from exc
        if Association.objects.exists():
            if not reset:
                raise CommandError("Sandbox data already exists; pass --reset to reload it.")
            if not getattr(settings, "HOMEOA_ALLOW_DEMO_RESET", False):
                raise CommandError(
                    "Refusing --reset: HOMEOA_ALLOW_DEMO_RESET is off in these settings."
                )
            names = ", ".join(a.name for a in Association.objects.all())
            self.stdout.write(
                f"Wiping all data tables (associations: {names}; users, ledger, registry, "
                "banking, billing, payments, audit) and reseeding."
            )
            demo.wipe()
        association = demo.seed(bare=bare)
        self.stdout.write(f"Seeded {association.name} ({'bare' if bare else 'full'}).")
