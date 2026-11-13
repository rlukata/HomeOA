from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.urls import reverse

from homeoa.accounts import services
from homeoa.accounts.management.e2e_guard import require_test_mode


class Command(BaseCommand):
    help = "Test only: print a single-use magic-link path for EMAIL."

    def add_arguments(self, parser):
        parser.add_argument("email")

    def handle(self, *args, email, **options):
        require_test_mode()
        user = get_user_model().objects.filter(email__iexact=email.strip()).first()
        if user is None:
            raise CommandError(f"No user with email {email}.")
        raw = services.create_magic_link(user)
        self.stdout.write(reverse("accounts:verify", args=[raw]))
