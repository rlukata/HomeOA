from django.core.management.base import BaseCommand

from homeoa.accounts import services
from homeoa.accounts.management.e2e_guard import require_test_mode


class Command(BaseCommand):
    help = "Test only: backdate every unused magic link by N minutes."

    def add_arguments(self, parser):
        parser.add_argument("--minutes", type=int, required=True)

    def handle(self, *args, minutes, **options):
        require_test_mode()
        count = services.age_outstanding_links(minutes)
        self.stdout.write(f"Aged {count} magic link(s) by {minutes} minutes.")
