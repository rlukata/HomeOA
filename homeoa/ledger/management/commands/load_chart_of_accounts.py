from django.core.management.base import BaseCommand, CommandError

from homeoa.core.models import Association
from homeoa.ledger.chart import load_chart_of_accounts


class Command(BaseCommand):
    help = "Create the HOA chart-of-accounts template and funds (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument("--association", type=int, help="Association id (default: all).")

    def handle(self, *args, **opts):
        qs = Association.objects.all()
        if opts["association"]:
            qs = qs.filter(pk=opts["association"])
            if not qs.exists():
                raise CommandError("No such association.")
        for assoc in qs:
            funds, accounts = load_chart_of_accounts(assoc)
            self.stdout.write(f"{assoc}: {funds} funds, {accounts} accounts created")
