"""Demo seed: verified fake bank accounts for the owners who sign in (E2E).

Only with the fake client (``HOMEOA_STRIPE_CLIENT=fake`` in dev/test/E2E settings); the
ids are fake-shaped and never reach Stripe. Owners of other units link a bank account
through the real flow (``bank.py``) against the fake.
"""

from __future__ import annotations

from django.conf import settings
from django.utils import timezone

from .client import fake_allowed
from .fake import FAKE_BANK, FAKE_LAST4
from .mandates import record_mandate
from .models import Verification


def seed_bank_accounts(association, unit_numbers) -> list:
    if getattr(settings, "HOMEOA_STRIPE_CLIENT", "live") != "fake" or not fake_allowed():
        return []
    from homeoa.core.context import association_today
    from homeoa.registry.models import Unit
    from homeoa.registry.services import current_owners

    today = association_today(association)
    out = []
    for number in unit_numbers:
        unit = Unit.all_associations.filter(association=association, number=number).first()
        if unit is None:
            continue
        for owner in current_owners(unit, today):
            if owner.user_id is None:
                continue
            tag = f"seed_{association.pk}_{number}_{owner.pk}"
            out.append(
                record_mandate(
                    unit,
                    owner,
                    FAKE_LAST4,
                    user=owner.user,
                    ip_address="127.0.0.1",
                    user_agent="HomeOA demo seed",
                    stripe_mandate_id=f"mandate_{tag}",
                    stripe_customer_id=f"cus_{tag}",
                    stripe_payment_method_id=f"pm_{tag}",
                    bank_name=FAKE_BANK,
                    verification=Verification.INSTANT,
                    verified_at=timezone.now(),
                )
            )
    return out
