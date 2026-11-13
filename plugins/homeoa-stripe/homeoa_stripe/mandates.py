"""ACH debit authorizations (mandates) with versioned authorization text.

``record_mandate(unit, owner, bank_last4, ...)`` stores what the owner accepted, when and
from where; ``revoke_mandate(mandate)`` stops future debits. A revoked mandate can't be
used for a payment. ``bank.py`` drives the Stripe side (SetupIntent, verification) and calls these.

The authorization wording is a DRAFT pending review (NACHA authorization requirements and
the association's counsel); each change gets a new version key and old versions stay here
so every stored mandate keeps naming the exact text it was given.
"""

from __future__ import annotations

import datetime as dt

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from homeoa import audit

from .models import StripeMandate

AUTHORIZATION_TEXTS = {
    "2026-10-v1": gettext_lazy(
        "I authorize %(association)s to debit the bank account ending in %(last4)s for "
        "payments I approve for unit %(unit)s, and, if an entry is made in error, to correct "
        "it. This authorization stays in effect until I cancel it in HomeOA or by notifying "
        "the association, in time for it to act before the next debit."
    ),
    "2026-10-v2": gettext_lazy(
        "I authorize %(association)s to debit the bank account ending in %(last4)s for "
        "payments I approve for unit %(unit)s, and, if an entry is made in error, to correct "
        "it. If I choose a fixed amount, I agree that each debit may be any amount from $0.01 "
        "up to that fixed amount, and I will not receive advance notice of the amount unless "
        "it falls outside that range. This authorization stays in effect until I cancel it in "
        "HomeOA or by notifying the association, in time for it to act before the next debit."
    ),
}
CURRENT_VERSION = "2026-10-v2"
#: Nacha: keep an authorization 2 years after it ends.
RETENTION = dt.timedelta(days=2 * 366)


class MandateError(ValueError):
    """``str(err)`` is user-facing."""


def authorization_text(unit, bank_last4: str, version: str = CURRENT_VERSION) -> str:
    return str(AUTHORIZATION_TEXTS[version]) % {
        "association": unit.association.name,
        "last4": bank_last4,
        "unit": unit.number,
    }


def record_mandate(
    unit,
    owner,
    bank_last4: str,
    *,
    user=None,
    ip_address: str | None = None,
    user_agent: str = "",
    stripe_mandate_id: str = "",
    stripe_customer_id: str = "",
    stripe_payment_method_id: str = "",
    setup_intent_id: str = "",
    bank_name: str = "",
    verification: str = "",
    verified_at=None,
    version: str = CURRENT_VERSION,
    accepted_at=None,
) -> StripeMandate:
    last4 = str(bank_last4 or "").strip()
    if len(last4) != 4 or not last4.isdigit():
        raise MandateError(_("Enter the last 4 digits of the bank account."))
    if version not in AUTHORIZATION_TEXTS:
        raise MandateError(_("Unknown authorization version."))
    if owner.association_id != unit.association_id:
        raise MandateError(_("That person is not in this association."))
    mandate = StripeMandate.all_associations.create(
        association=unit.association,
        unit=unit,
        owner=owner,
        user=user if getattr(user, "is_authenticated", False) else None,
        stripe_mandate_id=stripe_mandate_id[:64],
        stripe_customer_id=stripe_customer_id[:64],
        stripe_payment_method_id=stripe_payment_method_id[:64],
        setup_intent_id=setup_intent_id[:64],
        bank_name=(bank_name or "")[:100],
        verification=verification,
        verified_at=verified_at,
        bank_last4=last4,
        authorization_version=version,
        authorization_text=authorization_text(unit, last4, version),
        accepted_at=accepted_at or timezone.now(),
        ip_address=ip_address or None,
        user_agent=(user_agent or "")[:500],
    )
    audit.record("stripe.mandate_accepted", mandate, association=unit.association)
    return mandate


def revoke_mandate(mandate: StripeMandate, *, user=None) -> StripeMandate:
    with transaction.atomic():
        mandate = StripeMandate.all_associations.select_for_update().get(pk=mandate.pk)
        if mandate.revoked_at is None:
            mandate.revoked_at = timezone.now()
            mandate.save(update_fields=["revoked_at"])
            audit.record(
                "stripe.mandate_revoked",
                mandate,
                association=mandate.association,
                actor=user if getattr(user, "is_authenticated", False) else None,
            )
    return mandate


def purgeable(queryset=None, *, now=None):
    """Mandates a retention purge may remove: revoked at least 2 years ago.

    No purge exists today and ``StripeMandate.delete()`` refuses; this is the only set a
    future purge may consider. Active mandates are never purgeable.
    """
    now = now or timezone.now()
    qs = StripeMandate.all_associations.all() if queryset is None else queryset
    return qs.filter(revoked_at__isnull=False, revoked_at__lte=now - RETENTION)
