from django.core.management.base import BaseCommand, CommandError
from django.test import RequestFactory

from homeoa.accounts.management.e2e_guard import require_test_mode
from homeoa.notify import bounces
from homeoa.notify.models import Delivery
from homeoa.plugins.views import webhook


class Command(BaseCommand):
    help = (
        "Test only: bounce the latest email sent to ADDRESS by posting a signed bounce to "
        "the real webhook handler."
    )

    def add_arguments(self, parser):
        parser.add_argument("email")
        parser.add_argument("--reason", default="550 5.1.1 mailbox unavailable")

    def handle(self, *args, email, reason, **options):
        require_test_mode()
        delivery = (
            Delivery.all_associations.filter(channel="email", address__iexact=email.strip())
            .exclude(provider_id="")
            .select_related("association")
            .order_by("-sent_at", "-pk")
            .first()
        )
        if delivery is None:
            raise CommandError(f"No sent email to {email}.")
        association = delivery.association
        inst = bounces.ensure_enabled(association)
        body = bounces.payload_for(delivery, reason=reason)
        request = RequestFactory().post(
            f"/hooks/{bounces.PLUGIN_ID}/{association.pk}/",
            data=body,
            content_type="application/json",
            headers={bounces.SIGNATURE_HEADER: bounces.sign(body, inst.secrets["secret"])},
        )
        response = webhook(request, plugin_id=bounces.PLUGIN_ID, association_id=association.pk)
        if response.status_code != 200:
            raise CommandError(f"Bounce webhook answered {response.status_code}.")
        delivery.refresh_from_db()
        self.stdout.write(f"Delivery {delivery.pk} to {email}: {delivery.status}.")
